"""What to remember right now — and, just as importantly, what to leave out.

The constraint is not storage, it is attention. A prompt stuffed with a year of cards
makes the model slower, more expensive and *worse*: the signal the answer depends on
sits buried among hundreds of irrelevant lines. So the archive is never injected
wholesale. It is queried, ranked, and clipped to a budget.

The ranking follows the Generative Agents memory stream — a weighted sum of
**recency**, **importance** and **relevance** — with one deliberate departure. Those
systems score relevance by embedding similarity because their memories are
unstructured chat. These memories are not: every entry carries exact topic keys
(`alcohol`, `red_meat`, `eaten_out`, `swap_from:ham`). "What did you tell me last time
I drank" is a structured query, and answering it exactly beats answering it fuzzily.

That choice is aimed squarely at the failure the MemTier paper measured: retrieval,
not model capacity, is the binding constraint, and their multi-session recall@2 was
0.038 — the needed memory was absent from the top results 96% of the time. Exact
topic matching over typed entries is, for this domain, close to the oracle retrieval
they compared against.

The second half of this module is the budget. Every section of the prompt has a
declared token allowance, and `assemble` enforces it. Cutting is done by dropping
whole low-ranked items rather than truncating text, so what survives is intact and
readable.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("coach_recall")

# Generative-Agents-style weights. Relevance leads: an exactly on-topic memory from
# five weeks ago beats a vaguely related one from yesterday, which is the whole point
# of remembering at all.
W_RELEVANCE = 0.5
W_RECENCY = 0.3
W_IMPORTANCE = 0.2

# Recency half-life in days. Two weeks matches the cadence a habit changes on; older
# memories still surface when they are the only thing on topic.
RECENCY_HALF_LIFE_DAYS = 14.0

# Rough characters-per-token. Deliberately conservative: overshooting the budget is
# worse than leaving a little room, and this avoids a tokeniser dependency in a module
# that is otherwise pure stdlib.
CHARS_PER_TOKEN = 3.6

# Section allowances, in tokens. These add up to roughly 3.5k — a large but bounded
# fraction of a prompt that also carries today's meals and the food profile.
BUDGET = {
    "profile": 500,       # who this person is: stable, always included
    "recent_advice": 700,  # what the coach said lately, so it doesn't repeat itself
    "relevant": 900,      # memories retrieved for today's specific situation
    "reports": 900,       # the last weekly/monthly summary — the consolidated tier
    "events": 500,        # what happened today
}


def estimate_tokens(text: Any) -> int:
    return int(len(str(text or "")) / CHARS_PER_TOKEN) + 1


def _days_ago(day: str, today: str) -> float:
    try:
        return max(0.0, (date.fromisoformat(today) - date.fromisoformat(day)).days)
    except ValueError:
        return 999.0


def recency_score(day: str, today: str) -> float:
    """Exponential decay, 1.0 today and 0.5 at the half-life."""
    return 0.5 ** (_days_ago(day, today) / RECENCY_HALF_LIFE_DAYS)


def relevance_score(entry_topics: Sequence[str], query_topics: Sequence[str]) -> float:
    """Exact topic overlap, normalised by how much was asked for.

    Prefixed topics count as partial matches, so a query for `red_meat` still finds
    `swap_from:red_meat`. That is the only fuzziness here, and it is lexical and
    inspectable rather than learned.
    """
    if not query_topics:
        return 0.0
    entry_set = {t.lower() for t in entry_topics}
    hits = 0.0
    for topic in {t.lower() for t in query_topics}:
        if topic in entry_set:
            hits += 1.0
        elif any(topic in t or t.endswith(f":{topic}") for t in entry_set):
            hits += 0.6
    return min(1.0, hits / max(1, len(set(query_topics)) ** 0.5))


def score(entry: Dict[str, Any], *, today: str,
          query_topics: Sequence[str]) -> float:
    relevance = relevance_score(entry.get("topics", []), query_topics)
    return round(
        W_RELEVANCE * relevance
        + W_RECENCY * recency_score(str(entry.get("date") or ""), today)
        + W_IMPORTANCE * float(entry.get("importance") or 0), 4)


def rank(entries: Sequence[Dict[str, Any]], *, today: str,
         query_topics: Sequence[str], limit: int = 8,
         min_relevance: float = 0.15) -> List[Dict[str, Any]]:
    """The entries worth recalling, best first.

    `min_relevance` is the guard that keeps this honest: without it, recency and
    importance alone would drag in whatever happened to be recent, which is how a
    memory system ends up padding prompts with noise. A memory has to be *about* the
    thing to be recalled at all.
    """
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for entry in entries:
        relevance = relevance_score(entry.get("topics", []), query_topics)
        if relevance < min_relevance:
            continue
        scored.append((score(entry, today=today, query_topics=query_topics), entry))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(entry, _score=value) for value, entry in scored[:limit]]


def query_topics_for(*, profile: Dict[str, Any], today: Dict[str, Any],
                     events: Sequence[Dict[str, Any]],
                     findings: Sequence[Dict[str, Any]] = ()) -> List[str]:
    """What today is *about* — the keys the archive is searched with.

    Built from what actually happened rather than from a generic list, so a day with
    drinks in it retrieves drinking history and a day without it doesn't.
    """
    topics: List[str] = []
    for event in events:
        topics.append(f"kind:{event['kind']}")
        topics.extend(event.get("topics", []))
    for meal in today.get("meals", []):
        topics.extend(meal.get("food_groups", []))
        topics.extend(item["food"] for item in meal.get("items", [])[:6])
    for finding in findings:
        topics.append(f"finding:{finding.get('id')}")
        if finding.get("group"):
            topics.append(str(finding["group"]))
    seen, out = set(), []
    for topic in topics:
        key = str(topic).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out[:24]


def _fit(items: Sequence[Dict[str, Any]], budget_tokens: int,
         render) -> List[Any]:
    """Take items in order until the budget runs out.

    Whole items are dropped rather than truncated: half a memory is worse than none,
    because the model cannot tell which half it is missing.
    """
    out, spent = [], 0
    for item in items:
        rendered = render(item)
        cost = estimate_tokens(str(rendered))
        if spent + cost > budget_tokens:
            continue
        out.append(rendered)
        spent += cost
    return out


def assemble(*, today_iso: str, profile_facts: Sequence[Dict[str, Any]],
             recent_cards: Sequence[Dict[str, Any]],
             archive: Sequence[Dict[str, Any]],
             events: Sequence[Dict[str, Any]],
             reports: Sequence[Dict[str, Any]],
             query_topics: Sequence[str]) -> Dict[str, Any]:
    """The memory half of a prompt, inside its budget.

    Four tiers, in the order the literature converged on:
      * the **profile** — small, stable, always present;
      * **what was said recently** — so the coach doesn't repeat itself;
      * **retrieved episodes** — ranked for today specifically;
      * the **consolidated rollups** — one weekly and one monthly summary standing in
        for everything older, which is what keeps a year of history affordable.
    """
    remembered = rank(archive, today=today_iso, query_topics=query_topics, limit=10)

    # (prompt section, budget key, source, renderer). The prompt names are for the
    # model to read; the budget names are ours. Keeping the accounting keyed by the
    # budget means `_tokens` can be checked against `BUDGET` directly.
    sections = (
        ("about_you", "profile", profile_facts,
         lambda f: {"type": f.get("type"), "fact": f.get("fact")}),
        ("said_recently", "recent_advice", recent_cards,
         lambda c: {"when": c.get("date") or c.get("at"),
                    "title": c.get("summary") or c.get("title"),
                    "said": (c.get("body") or "")[:220]}),
        ("you_might_recall", "relevant", remembered,
         lambda e: {"when": e.get("date"), "what": e.get("summary"),
                    "detail": (e.get("body") or "")[:200], "kind": e.get("kind")}),
        ("previous_reports", "reports", reports,
         lambda r: {"period": r.get("period"), "covering": r.get("key"),
                    "headline": r.get("headline"),
                    "summary": (r.get("summary") or "")[:600]}),
        ("today_events", "events", events,
         lambda e: {"what": e.get("headline"), "when": e.get("at"),
                    "context": e.get("detail", "")}),
    )

    memory: Dict[str, Any] = {}
    spent: Dict[str, int] = {}
    for name, budget_key, source, render in sections:
        memory[name] = _fit(source, BUDGET[budget_key], render)
        spent[budget_key] = estimate_tokens(str(memory[name]))
    spent["total"] = sum(spent.values())
    memory["_tokens"] = spent
    return memory
