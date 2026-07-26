"""Everything the coach has ever said or noticed, kept.

The feed is deliberately forgetful: a card expires and drops out, because a stale
"what to eat next" is worse than none. That is right for the screen and wrong for the
system — the weekly review should be able to read what was actually advised on
Tuesday, and a yearly report should be able to read the year.

So every card, every conversation turn, every notable event and every generated
report is appended here and never modified. This is the *episodic* tier: it is not
injected into prompts wholesale (that is the failure mode this design exists to
avoid), it is queried.

Layout — one shard per month, newline-delimited JSON:

    coach/archive/2026-07.jsonl        cards, chat turns, events
    coach/reports/weekly/2026-07-20.json     the rollups (see coach_reports)
    coach/reports/monthly/2026-07.json
    coach/reports/yearly/2026.json

A month of this user is roughly 90 cards, 200 events and a few dozen chat turns —
tens of kilobytes. Sharding by month means a weekly report reads one blob, a monthly
reads one or two, and a yearly reads twelve *reports* rather than a year of raw
entries. That hierarchy is the whole trick: each level summarises the level below, so
the context needed to write any report stays roughly constant no matter how many
years of history exist.

Entries are typed and carry `topics` — exact keys like `alcohol`, `red_meat`,
`eaten_out`, `finding:group_over:red_meat`. Retrieval matches on those keys rather
than on embeddings, which for this domain is not a compromise: "what did you tell me
last time I drank" is a structured query, and answering it exactly beats answering it
fuzzily. See `coach_recall`.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

import coach_store as store

log = logging.getLogger("coach_archive")

ROOT = f"{store.ROOT}/archive"
REPORTS = f"{store.ROOT}/reports"

# Entry kinds. `card` and `chat` are what the coach said; `event` is what happened;
# `report` is a rollup's headline (the full text lives in its own blob).
KINDS = ("card", "chat", "event", "report")


def shard_path(day: str) -> str:
    """The blob one day's entries live in (month granularity)."""
    return f"{ROOT}/{str(day)[:7]}.jsonl"


def report_path(period: str, key: str) -> str:
    return f"{REPORTS}/{period}/{key}.json"


# -- writing -------------------------------------------------------------------

def append(entries: Sequence[Dict[str, Any]]) -> int:
    """Append entries to their month shards. Returns how many were written.

    Grouped by shard so one generation's worth of cards costs one read-modify-write,
    and done under a generation precondition so a scheduled run and a live chat turn
    landing together cannot lose each other's lines.
    """
    if not entries:
        return 0
    by_shard: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            continue
        by_shard.setdefault(shard_path(entry.get("date") or ""), []).append(entry)

    written = 0
    for path, group in by_shard.items():
        def add(current: Any, group=group) -> Dict[str, Any]:
            lines = list((current or {}).get("entries") or [])
            known = {e.get("id") for e in lines if isinstance(e, dict)}
            for entry in group:
                # Ids make appending idempotent: a Cloud Tasks retry re-posts the same
                # cards, and the archive must not grow a duplicate for each attempt.
                if entry.get("id") and entry["id"] in known:
                    continue
                lines.append(entry)
                known.add(entry.get("id"))
            return {"entries": lines}
        result = store.update_json(path, add, default={"entries": []})
        written += len((result or {}).get("entries") or [])
    return written


def entry(kind: str, *, day: str, at: str, id: str, summary: str,
          topics: Sequence[str] = (), importance: float = 0.3,
          body: str = "", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One archive line. `summary` is what a future model will read; `data` is the
    structured detail it can drill into."""
    return {
        "kind": kind,
        "id": id,
        "date": day,
        "at": at,
        "summary": summary[:400],
        "body": body[:1500],
        "topics": sorted({t for t in topics if t})[:12],
        "importance": round(min(max(importance, 0.0), 1.0), 2),
        "data": data or {},
    }


def record_cards(cards: Sequence[Dict[str, Any]], *, now: datetime) -> int:
    """Archive a generation's cards. Called every time the feed is written, so the
    record of what the coach advised is complete even though the feed itself is not."""
    entries = []
    for card in cards:
        topics = ["card", f"kind:{card.get('kind')}"]
        if card.get("topic"):
            topics.append(str(card["topic"]))
        if isinstance(card.get("evidence"), dict) and card["evidence"].get("finding"):
            topics.append(f"finding:{card['evidence']['finding']}")
        swap = card.get("swap") or {}
        if swap.get("from"):
            topics += [f"swap_from:{swap['from']}", f"swap_to:{swap.get('to')}"]
        entries.append(entry(
            "card", day=str(card.get("date") or now.date().isoformat()),
            at=str(card.get("created_at") or now.isoformat(timespec="seconds"))[11:16],
            id=f"card:{card.get('id')}", summary=str(card.get("title") or ""),
            body=str(card.get("body") or ""), topics=topics,
            # A card the coach chose to show is worth more than a routine one; the
            # priority it was given is the best available proxy.
            importance=min(0.9, float(card.get("priority") or 50) / 110),
            data={"kind": card.get("kind"), "swap": card.get("swap"),
                  "source": card.get("source"),
                  "plates": [p.get("title") for p in (card.get("plates") or [])]}))
    return append(entries)


def record_events(events: Sequence[Dict[str, Any]]) -> int:
    return append([
        entry("event", day=event["date"], at=event.get("at", ""),
              id=f"event:{event['date']}:{event['kind']}:{event.get('at', '')}",
              summary=event["headline"], body=event.get("detail", ""),
              topics=["event", f"kind:{event['kind']}", *event.get("topics", [])],
              importance=event["importance"], data=event.get("evidence", {}))
        for event in events])


def record_chat(thread_id: str, *, day: str, at: str, question: str,
                answer: str, card_title: str = "") -> int:
    """Archive one exchange. Conversations are where the user says things the log can
    never show — why they ate what they ate, what they are willing to change."""
    return append([entry(
        "chat", day=day, at=at, id=f"chat:{thread_id}:{at}",
        summary=question[:200], body=answer,
        topics=["chat", f"thread:{thread_id}"] + (
            [f"about:{card_title[:40]}"] if card_title else []),
        # Something the user asked about unprompted is a strong signal of what they
        # care about, and worth surfacing again later.
        importance=0.65,
        data={"thread": thread_id, "question": question, "answer": answer})])


def record_report(period: str, key: str, *, day: str, headline: str,
                  topics: Sequence[str] = ()) -> int:
    return append([entry(
        "report", day=day, at="", id=f"report:{period}:{key}",
        summary=headline, topics=["report", f"period:{period}", *topics],
        importance=0.95, data={"period": period, "key": key})])


# -- reading -------------------------------------------------------------------

def read_range(start: str, end: str, *, kinds: Sequence[str] = ()
               ) -> List[Dict[str, Any]]:
    """Every entry with `start <= date <= end`, oldest first.

    Reads only the month shards the range touches — a week costs one or two blobs.
    """
    out: List[Dict[str, Any]] = []
    for path in _shards_between(start, end):
        payload = store.read_json(path, default=None)
        for line in ((payload or {}).get("entries") or []):
            if not isinstance(line, dict):
                continue
            if not (start <= str(line.get("date") or "") <= end):
                continue
            if kinds and line.get("kind") not in kinds:
                continue
            out.append(line)
    out.sort(key=lambda e: (str(e.get("date")), str(e.get("at"))))
    return out


def _shards_between(start: str, end: str) -> List[str]:
    try:
        first = date.fromisoformat(start).replace(day=1)
        last = date.fromisoformat(end).replace(day=1)
    except ValueError:
        return []
    months, cursor = [], first
    while cursor <= last and len(months) < 400:
        months.append(f"{ROOT}/{cursor.isoformat()[:7]}.jsonl")
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


def save_report(period: str, key: str, payload: Dict[str, Any]) -> None:
    store.write_json(report_path(period, key), payload)


def load_report(period: str, key: str) -> Optional[Dict[str, Any]]:
    return store.read_json(report_path(period, key), default=None)


def recent_reports(period: str, *, before: str, limit: int = 12
                   ) -> List[Dict[str, Any]]:
    """The most recent stored reports of a period, newest first.

    This is what makes a monthly report cheap: it reads four or five weeklies, not a
    month of meals. A yearly reads twelve monthlies. The cost of writing any report
    stays flat as the history grows.
    """
    names = store.list_names(f"{REPORTS}/{period}/")
    keys = sorted((n.rsplit("/", 1)[-1][:-5] for n in names if n.endswith(".json")),
                  reverse=True)
    out = []
    for key in keys:
        if key >= before:
            continue
        report = load_report(period, key)
        if report:
            out.append(report)
        if len(out) >= limit:
            break
    return out


def stats() -> Dict[str, Any]:
    """What the archive holds — for the debug endpoint and for reassuring a human
    that nothing is being thrown away."""
    shards = store.list_names(f"{ROOT}/")
    counts: Dict[str, int] = {}
    months = []
    for path in shards:
        payload = store.read_json(path, default=None)
        entries = (payload or {}).get("entries") or []
        months.append({"month": path.rsplit("/", 1)[-1][:-6], "entries": len(entries)})
        for line in entries:
            if isinstance(line, dict):
                counts[line.get("kind", "?")] = counts.get(line.get("kind", "?"), 0) + 1
    return {"months": sorted(months, key=lambda m: m["month"]),
            "by_kind": counts,
            "total": sum(counts.values()),
            "reports": {period: len(store.list_names(f"{REPORTS}/{period}/"))
                        for period in ("weekly", "monthly", "yearly")}}
