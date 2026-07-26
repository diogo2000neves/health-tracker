"""The coach's feed: which cards exist right now, and why.

The old coach was one page that regenerated itself while the user watched. This is
the opposite shape — a stream of small cards, each written once in the background
and valid for a known window:

    07:30  day_plan     "Ontem o jantar foi arroz e carne; hoje mete legumes ao almoço"
    07:30  next_meal    three breakfast plates
    15:30  check_in     "Já levas 62 g de proteína e nenhum legume — o lanche dá para isso"
    15:30  next_meal    three afternoon plates
    21:30  day_summary  "Dia fechado com 1 890 kcal; a fruta apareceu duas vezes"
    Sun    weekly_review the week's one focus, food-first
    any    pattern      "Peixe: nada registado em 12 dias"

Two rules hold the whole thing together.

**The model writes sentences; this module decides facts.** `generate_cards` plans
which cards a slot should carry from the deterministic food profile, hands the facts
to a narrator callable, and then re-attaches the ids, evidence, priorities and
expiry itself. Anything the model returns that isn't traceable to the facts it was
given is dropped in `_validated_swap` — the model cannot suggest replacing a food
the user has never logged, which is the exact failure the old critic pass caught in
production ("swap your white bread" when no bread was ever logged).

**Nothing repeats until it has earned it.** A pattern finding that was shown goes
into `state["shown"]` with the date and can't come back for `PATTERN_COOLDOWN_DAYS`
unless it got materially worse. A feed that says the same thing every day trains the
user to stop opening it.

The callables (`narrate`, `plates_fn`) are injected, so every branch here is
testable without a network and the caller owns which model runs.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import food_patterns as fp
import food_taxonomy as tax

log = logging.getLogger("coach_feed")

# The generation slots. `adhoc` is a refresh triggered by the app or by a meal
# landing — it only ever refreshes the time-sensitive cards.
SLOTS = ("morning", "afternoon", "evening", "weekly", "adhoc")

# How long a shown pattern finding stays off the feed. Eight days means a weekly
# habit gets one mention per week at most.
PATTERN_COOLDOWN_DAYS = 8

# A finding may jump its cooldown if it got this much worse (absolute severity).
SEVERITY_ESCALATION = 0.2

# At most this many cards live in the feed at once — a feed you scroll is a feed you
# skim, and the whole point is that the top card is worth reading.
MAX_FEED_CARDS = 8

# Per-kind lifetime. A card outliving its facts is worse than no card: "o lanche dá
# para isso" at 23:00 is noise.
_TTL_HOURS = {
    # Five hours bridges the gap between the scheduled slots (07:30 / 15:30 / 21:30)
    # from either side, so there is no stretch of the day with no answer to "what do I
    # eat next" — and it stays short enough that the answer still fits the day.
    "next_meal": 5,
    "day_plan": 14,
    "check_in": 7,
    "day_summary": 13,
    "pattern": 72,
    "weekly_review": 8 * 24,
    "win": 48,
}

# Ordering in the feed. Time-sensitive first, then the week's frame, then the slow
# observations.
_PRIORITY = {
    "next_meal": 100,
    "check_in": 92,
    "day_plan": 90,
    "day_summary": 88,
    "weekly_review": 80,
    "pattern": 60,
    "win": 40,
}


# -- planning ------------------------------------------------------------------

def _wants(slot: str) -> Tuple[str, ...]:
    """Which card kinds a slot generates."""
    if slot == "morning":
        return ("day_plan", "next_meal", "pattern")
    if slot == "afternoon":
        return ("check_in", "next_meal", "pattern")
    if slot == "evening":
        return ("day_summary", "win", "pattern")
    if slot == "weekly":
        return ("weekly_review", "pattern")
    return ("next_meal", "check_in")          # adhoc


def eligible_findings(profile: Dict[str, Any], state: Dict[str, Any], *,
                      today: str, limit: int = 2) -> List[Dict[str, Any]]:
    """The findings a feed may show today: highest severity first, minus anything
    still inside its cooldown (unless it has got materially worse)."""
    shown = (state or {}).get("shown") or {}
    out: List[Dict[str, Any]] = []
    for finding in profile.get("findings", []):
        record = shown.get(finding["id"])
        if isinstance(record, dict):
            days = _days_between(str(record.get("date") or ""), today)
            worse = finding["severity"] - float(record.get("severity") or 0)
            if days is not None and days < PATTERN_COOLDOWN_DAYS \
                    and worse < SEVERITY_ESCALATION:
                continue
        out.append(finding)
        if len(out) >= limit:
            break
    return out


def build_facts(*, slot: str, now: datetime, profile: Dict[str, Any],
                today: Dict[str, Any], nutrients: Dict[str, Any],
                memory: Dict[str, Any], findings: Sequence[Dict[str, Any]],
                weekly: Optional[Dict[str, Any]] = None,
                recent_titles: Sequence[str] = ()) -> Dict[str, Any]:
    """The prompt payload: food patterns FIRST, nutrients as supporting evidence.

    That ordering is the entire content fix. The old prompt led with a nutrient
    table, so the model could only ever produce nutrient advice; here the model sees
    "fries four times this week, no fish in twelve days" and the nutrient numbers
    only as corroboration.
    """
    groups = profile.get("groups", {})
    interesting = {
        key: {
            "servings_per_week": rec.get("servings_per_week"),
            "reference_min": rec.get("week_min"),
            "reference_max": rec.get("week_max"),
            "days_since_last": rec.get("days_since_last"),
            "top_foods": rec.get("top_foods", []),
        }
        for key, rec in groups.items()
        if rec.get("week_min") or rec.get("week_max") or rec.get("occurrences")
    }
    return {
        "slot": slot,
        "now": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "food_patterns": {
            "days_logged": profile.get("days_logged"),
            "groups": interesting,
            "variety": profile.get("variety"),
            "meal_slots": profile.get("slots"),
            "streaks": profile.get("streaks", [])[:3],
        },
        "findings": [
            {"id": f["id"], "kind": f["kind"], "fact": f["headline"],
             "evidence": f["evidence"], "foods": f["foods"],
             "swap_options": profile.get("swaps", {}).get(f["id"], {})}
            for f in findings
        ],
        "today": today,
        "nutrients_supporting": nutrients,
        "foods_the_user_eats": [
            {"food": f["food"], "group": f["group"], "times": f["times"],
             "typical_portion_g": f["median_portion_g"], "usually_at": f["slot_label"]}
            for f in profile.get("foods", [])[:25]
        ],
        "memory": (memory or {}).get("facts", [])[:20],
        "weekly": weekly,
        "already_said_recently": list(recent_titles)[:6],
    }


# -- assembly ------------------------------------------------------------------

def card_id(*, date: str, kind: str, topic: str = "") -> str:
    """A stable id: same day + same kind + same topic is the SAME card, so a re-run
    replaces it instead of stacking a duplicate.

    Deliberately NOT keyed on the slot. "What to eat next" is one question with one
    current answer, so the 15:30 run and the refresh triggered by a logged lunch must
    overwrite each other — keyed by slot they would sit in the feed side by side, two
    cards with the same title telling the user different things.
    """
    tail = f":{topic}" if topic else ""
    return f"{date}:{kind}{tail}"


def thread_id_for(card: Dict[str, Any]) -> str:
    """A deterministic thread id per card, so opening the chat twice continues one
    conversation instead of forking. Hashed to stay path-safe."""
    digest = hashlib.sha256(card["id"].encode("utf-8")).hexdigest()[:16]
    return f"t-{digest}"


def _expires(kind: str, now: datetime) -> str:
    return (now + timedelta(hours=_TTL_HOURS.get(kind, 24))).isoformat(
        timespec="seconds")


def _clip(text: Any, limit: int) -> str:
    out = " ".join(str(text or "").split())
    return out[:limit].rstrip()


def _logged_food_names(profile: Dict[str, Any]) -> Dict[str, str]:
    """Canonical -> display name for everything in the log, plus the raw names the
    log used, so a model answer can be matched however it phrased the food."""
    names: Dict[str, str] = {}
    for food in profile.get("foods", []):
        names[tax.normalize(food["food"])] = food["food"]
        for raw in food.get("raw_names", []):
            names[tax.normalize(raw)] = food["food"]
    return names


def _validated_swap(raw: Any, *, profile: Dict[str, Any],
                    finding: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A swap the facts actually support, or None.

    `from` must be a food the user has logged — the guard against the coach telling
    someone to cut a food they never ate. `to` must be one of the candidates the
    finding offered, so the replacement is either already in their diet or an
    explicitly-flagged new staple.
    """
    if not isinstance(raw, dict):
        return None
    src = _clip(raw.get("from"), 60)
    dst = _clip(raw.get("to"), 60)
    if not src or not dst:
        return None

    logged = _logged_food_names(profile)
    src_key = tax.normalize(src)
    if src_key not in logged:
        # Try the canonical form ("pão branco" -> "white bread") before giving up.
        src_key = tax.canonical_name(src)
        if src_key not in logged:
            log.info("dropping swap: %r is not in the log", src)
            return None

    options = (profile.get("swaps", {}).get(finding["id"], {}) if finding else {})
    allowed = {tax.normalize(o["food"]): o for o in options.get("to", [])}
    match = allowed.get(tax.normalize(dst)) or allowed.get(tax.canonical_name(dst))
    if not match:
        log.info("dropping swap: %r was not among the offered options", dst)
        return None

    return {"from": logged[src_key], "to": match["food"],
            "why": _clip(raw.get("why"), 200), "new": bool(match.get("new"))}


def _chips(raw: Any, limit: int = 3) -> List[Dict[str, str]]:
    chips: List[Dict[str, str]] = []
    if isinstance(raw, list):
        for entry in raw[:limit]:
            if isinstance(entry, dict):
                text = _clip(entry.get("label"), 28)
                tone = str(entry.get("tone") or "neutral")
            else:
                text, tone = _clip(entry, 28), "neutral"
            if text:
                chips.append({"label": text,
                              "tone": tone if tone in ("good", "warn", "bad",
                                                       "neutral") else "neutral"})
    return chips


def _card(*, kind: str, slot: str, date: str, now: datetime, title: str, body: str,
          topic: str = "", chips: Sequence[Dict[str, str]] = (),
          evidence: Optional[Dict[str, Any]] = None,
          swap: Optional[Dict[str, Any]] = None,
          plates: Optional[List[Dict[str, Any]]] = None,
          priority_bonus: float = 0.0,
          next_slot: str = "") -> Dict[str, Any]:
    card = {
        "id": card_id(date=date, kind=kind, topic=topic),
        "kind": kind,
        "slot": slot,
        "date": date,
        "topic": topic or None,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": _expires(kind, now),
        "priority": round(_PRIORITY.get(kind, 50) + priority_bonus, 2),
        "title": title,
        "body": body,
        "chips": list(chips),
        "evidence": evidence or {},
        "swap": swap,
        "plates": plates or None,
        "next_slot": next_slot or None,
    }
    card["thread_id"] = thread_id_for(card)
    return card


# -- generation ----------------------------------------------------------------

def generate_cards(*, slot: str, now: datetime, profile: Dict[str, Any],
                   today: Dict[str, Any], nutrients: Dict[str, Any],
                   memory: Dict[str, Any], state: Dict[str, Any],
                   narrate: Callable[[Dict[str, Any]], Dict[str, Any]],
                   plates_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                   weekly: Optional[Dict[str, Any]] = None,
                   recent_titles: Sequence[str] = ()
                   ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build this slot's cards. Returns (cards, findings_shown).

    A narration failure is not fatal: the deterministic cards that don't need prose
    (the plates) still ship, and the caller keeps whatever the feed already had. The
    coach going quiet for one slot is recoverable; the coach showing a spinner
    forever is not.
    """
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}")

    date = now.date().isoformat()
    wants = _wants(slot)
    findings = (eligible_findings(profile, state, today=date)
                if "pattern" in wants else [])

    cards: List[Dict[str, Any]] = []

    # 1. The plates. Deterministic context in, model prose out, and it is generated
    #    FIRST because it is the card the user opens the app for.
    if "next_meal" in wants and plates_fn is not None:
        try:
            result = plates_fn() or {}
            plates = [p for p in (result.get("plates") or []) if isinstance(p, dict)]
            if plates:
                next_slot = _clip(result.get("next_slot"), 30)
                cards.append(_card(
                    kind="next_meal", slot=slot, date=date, now=now,
                    title=(f"O que comer ao {next_slot.lower()}" if next_slot
                           else "O que comer a seguir"),
                    body=_clip(result.get("reasoning"), 240),
                    chips=[{"label": f"{len(plates)} ideias", "tone": "neutral"}],
                    evidence={"calories_left": today.get("calories_left"),
                              "protein_left_g": today.get("protein_left_g")},
                    plates=plates, next_slot=next_slot))
        except Exception:
            log.exception("plate generation failed (non-fatal)")

    # 2. The prose cards, in one call.
    facts = build_facts(slot=slot, now=now, profile=profile, today=today,
                        nutrients=nutrients, memory=memory, findings=findings,
                        weekly=weekly, recent_titles=recent_titles)
    prose_kinds = [k for k in wants if k not in ("next_meal", "pattern")]
    facts["wanted_cards"] = prose_kinds + (["pattern"] if findings else [])

    written: List[Dict[str, Any]] = []
    try:
        answer = narrate(facts) or {}
        written = [c for c in (answer.get("cards") or []) if isinstance(c, dict)]
    except Exception:
        log.exception("card narration failed (non-fatal)")

    by_finding = {f["id"]: f for f in findings}
    shown: Dict[str, Any] = {}
    for entry in written:
        kind = str(entry.get("kind") or "").strip()
        ref = str(entry.get("ref") or "").strip()
        title = _clip(entry.get("title"), 70)
        body = _clip(entry.get("body"), 420)
        if not title or not body:
            continue

        finding = by_finding.get(ref)
        if kind == "pattern":
            if not finding:
                # A "pattern" card with no finding behind it is exactly the
                # untethered advice this rebuild exists to remove.
                log.info("dropping pattern card with unknown ref %r", ref)
                continue
            cards.append(_card(
                kind="pattern", slot=slot, date=date, now=now, title=title,
                body=body, topic=finding.get("group") or finding["kind"],
                chips=_chips(entry.get("chips")),
                evidence={"finding": finding["id"], "fact": finding["headline"],
                          **finding["evidence"]},
                swap=_validated_swap(entry.get("swap"), profile=profile,
                                     finding=finding),
                priority_bonus=10 * finding["severity"]))
            shown[finding["id"]] = {"date": date, "severity": finding["severity"]}
            continue

        if kind not in prose_kinds:
            continue
        cards.append(_card(
            kind=kind, slot=slot, date=date, now=now, title=title, body=body,
            chips=_chips(entry.get("chips")),
            evidence={"days_logged": profile.get("days_logged"),
                      "meals_today": len(today.get("meals", []))},
            swap=_validated_swap(entry.get("swap"), profile=profile,
                                 finding=findings[0] if findings else None)))

    return cards, shown


# -- the feed the app reads ----------------------------------------------------

def merge_cards(existing: Sequence[Dict[str, Any]],
                fresh: Sequence[Dict[str, Any]], *, now: datetime
                ) -> List[Dict[str, Any]]:
    """Fold new cards into what the feed already had: same id replaces, expired
    drops, newest-then-priority wins the cap.

    Replace-by-id is what makes a re-run idempotent — three refreshes in a morning
    leave one breakfast card, not three.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for card in list(existing) + list(fresh):
        if not isinstance(card, dict) or not card.get("id"):
            continue
        if _expired(card, now):
            continue
        merged[card["id"]] = card
    return feed_order(merged.values())[:MAX_FEED_CARDS]


def live_cards(cards: Sequence[Dict[str, Any]], *, now: datetime
               ) -> List[Dict[str, Any]]:
    """The cards still valid at `now`, feed order."""
    return feed_order(c for c in cards
                      if isinstance(c, dict) and not _expired(c, now))


def feed_order(cards) -> List[Dict[str, Any]]:
    """Highest priority first, newest first within a priority. Two stable passes
    rather than one composite key, because the recency half sorts descending on a
    string and there is nothing to negate."""
    by_recency = sorted(cards, key=lambda c: str(c.get("created_at") or ""),
                        reverse=True)
    return sorted(by_recency, key=lambda c: -float(c.get("priority") or 0))


def _expired(card: Dict[str, Any], now: datetime) -> bool:
    stamp = str(card.get("expires_at") or "")
    if not stamp:
        return False
    try:
        expires = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if (expires.tzinfo is None) != (now.tzinfo is None):
        expires = expires.replace(tzinfo=now.tzinfo)
    return expires <= now


def is_stale(cards: Sequence[Dict[str, Any]], *, now: datetime,
             max_age_hours: float = 6.0) -> bool:
    """Whether the newest card is old enough that the app should ask for a refresh.
    Used to decide between "read and render" and "read, render, and quietly kick a
    background generation" — never to block the screen."""
    newest = ""
    for card in cards:
        newest = max(newest, str(card.get("created_at") or ""))
    if not newest:
        return True
    try:
        created = datetime.fromisoformat(newest)
    except ValueError:
        return True
    if (created.tzinfo is None) != (now.tzinfo is None):
        created = created.replace(tzinfo=now.tzinfo)
    return (now - created).total_seconds() > max_age_hours * 3600


def slot_for(now: datetime) -> str:
    """The slot a clock time belongs to — used by the scheduler-free paths (a manual
    refresh, a meal landing) to pick which cards to regenerate."""
    hour = now.hour
    if hour < 11:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _days_between(then: str, ref: str) -> Optional[int]:
    from datetime import date as _date
    try:
        return (_date.fromisoformat(ref) - _date.fromisoformat(then)).days
    except ValueError:
        return None


# -- next-meal context ---------------------------------------------------------

def next_meal_candidates(profile: Dict[str, Any], *,
                         nutrient_candidates: Dict[str, Any],
                         slot_hint: str = "") -> Dict[str, Any]:
    """What the plates may be built from.

    The old generator only had candidates when a nutrient was below its floor, and
    returned "skipped" otherwise — which is how the app ended up with a sheet that
    said "preparing…" forever on a day that happened to be on target. "What do I eat
    next?" is always a fair question, so there is always material: the nutrient
    shortfalls when they exist, plus the foods that serve the current food-level
    findings, plus what the user usually eats at this slot.
    """
    out: Dict[str, Any] = {"by_nutrient": nutrient_candidates or {}}

    from_findings: List[Dict[str, Any]] = []
    for finding in profile.get("findings", [])[:3]:
        options = profile.get("swaps", {}).get(finding["id"], {})
        for entry in options.get("to", []):
            if entry["food"] not in [f["food"] for f in from_findings]:
                from_findings.append({**entry, "because": finding["headline"]})
    out["for_findings"] = from_findings[:8]

    usual = [f for f in profile.get("foods", [])
             if not slot_hint or f.get("top_slot") == slot_hint]
    out["usual_at_this_slot"] = [
        {"food": f["food"], "group": f["group"],
         "typical_portion_g": f["median_portion_g"], "times": f["times"]}
        for f in usual[:10]
    ]
    # Under-target groups worth pulling toward, with something concrete to reach for.
    out["groups_to_favour"] = [
        {"group": key, "label": rec["label"],
         "servings_per_week": rec.get("servings_per_week"),
         "reference_min": rec.get("week_min"),
         "options": [o["food"] for o in
                     fp.swap_candidates({"kind": "group_under", "group": key,
                                         "id": f"group_under:{key}"},
                                        profile.get("foods", []))["to"]][:3]}
        for key, rec in profile.get("groups", {}).items()
        if rec.get("week_min") and (rec.get("servings_per_week") or 0)
        < rec["week_min"] * fp.UNDER_RATIO
    ][:4]
    return out
