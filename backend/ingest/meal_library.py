"""Repeating and editing meals: the arithmetic of a portion, and the history the app
offers to log again.

This replaced the `templates` tab. A template was a named, frozen copy of one meal,
and it failed on the first real morning it met: breakfast is *the same meal* most
days, but not the same grams — 20 g of whey one day, 10 g the next, a banana some
days and not others. A frozen copy was either wrong or discarded, so in practice it
was discarded. Two things are true of how this user eats, and this module is built
on both:

  * **The history already is the library.** Every meal ever logged carries its
    ingredients, their grams and their full nutrient panel. Nothing has to be named
    or saved in advance; a meal eaten twice is a habit, and `families` finds it.
  * **The last version is the best guess, and it is one edit away.** The app offers
    the most recent member of a habit, pre-filled, and the grams are edited in
    place — `rescale` then moves every macro AND every micronutrient with them, so
    a changed portion is recorded exactly, never just relabelled.

Pure stdlib, no I/O, no model: `main.py` owns the sheet and the HTTP, and the name
canonicaliser (`food_taxonomy.canonical_name`) is injected, so every rule here is
unit-tested against plain dicts.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

MACRO_KEYS = ("calories", "protein_g", "carbs_g", "fat_g")

# An ingredient the app asked the model to estimate ("banana 120 g") while the rest
# of the meal was logged from history. It sits in the meal's `items` as a zero-value
# placeholder until the queue worker swaps in the estimate — so the meal is saved,
# visible and editable immediately, and a failure is visible rather than silent.
PENDING = "pending"
FAILED = "failed"
STATUSES = (PENDING, FAILED)

# Two meals are the same habit when at least half of their combined ingredients
# match. Measured on the live log: the usual breakfast with and without its banana
# (3 of 4) is one habit; the same oats and whey with an apple instead of a banana
# and no peanut butter (2 of 5) is another — which is the honest reading.
SIMILARITY = 0.5
# A habit is something eaten at least twice. A one-off stays reachable through the
# recent list and the search; it just isn't *suggested*.
FAMILY_MIN_MEALS = 2
# Recency weighting: a meal counts half as much every two weeks. Long enough that a
# weekly habit stays near the top, short enough that last month's diet fades.
RECENCY_HALF_LIFE_DAYS = 14.0
# Time-of-day affinity. At 09:30 breakfast should lead and dinner should not, but
# a habit eaten at a different hour must still be findable — hence a floor rather
# than a filter.
TIME_SCALE_MIN = 120.0
TIME_FLOOR = 0.3
# Versions of one habit offered as alternatives to its latest.
MAX_VERSIONS = 4


def is_placeholder(item: Dict[str, Any]) -> bool:
    return item.get("status") in STATUSES


def placeholder(text: str) -> Dict[str, Any]:
    """The zero-value stand-in for an ingredient the model is still estimating."""
    return {"name": text, "portion_g": 0.0, "calories": 0.0, "protein_g": 0.0,
            "carbs_g": 0.0, "fat_g": 0.0, "status": PENDING}


def _nonneg(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, number)


def _round_nutrient(key: str, value: float) -> float:
    # The same precision `main._normalize_nutrients` stores: grams to 2 dp, mg/ug
    # to 1 dp. Rounding here too keeps a rescaled item byte-identical to what a
    # fresh estimate of that portion would have been written as.
    return round(value, 2 if key.endswith("_g") else 1)


def rescale(item: Dict[str, Any], grams: Any) -> Dict[str, Any]:
    """`item` at a different portion: grams, every macro and every nutrient move
    together, in proportion.

    This is the whole fix for "I edited 100 g to 50 g and nothing changed": the
    old edit rewrote the label and left the numbers behind. Nutrients that round to
    nothing at the new portion are dropped, exactly as a fresh estimate would omit
    them. An item with no weight (a capsule, a pinch the model sized at 0 g) has no
    per-gram basis and is returned unchanged rather than scaled by infinity."""
    out = dict(item)
    target = _nonneg(grams)
    base = _nonneg(item.get("portion_g")) or 0.0
    if target is None or base <= 0 or abs(target - base) < 1e-9:
        return out
    factor = target / base
    out["portion_g"] = round(target, 1)
    for key in MACRO_KEYS:
        out[key] = round((_nonneg(item.get(key)) or 0.0) * factor, 1)
    nutrients = item.get("nutrients") or {}
    if isinstance(nutrients, dict) and nutrients:
        scaled = {}
        for key, value in nutrients.items():
            amount = _nonneg(value)
            if amount is None:
                continue
            amount = _round_nutrient(key, amount * factor)
            if amount > 0:
                scaled[key] = amount
        if scaled:
            out["nutrients"] = scaled
        else:
            out.pop("nutrients", None)
    return out


def override(item: Dict[str, Any], values: Any) -> Dict[str, Any]:
    """Hand-typed macros, for when the estimate itself was wrong (a label says 24 g
    of protein, the model said 30). Only the macros can be corrected by hand — there
    is no way to type a micronutrient panel — so those are left as they were."""
    out = dict(item)
    if not isinstance(values, dict):
        return out
    for key in MACRO_KEYS:
        if key in values:
            number = _nonneg(values[key])
            if number is not None:
                out[key] = round(number, 1)
    return out


# -- habits: which past meals are "the same meal" ---------------------------------
def _tokens(key: str) -> frozenset:
    return frozenset(key.split())


def _items_match(a: str, b: str) -> bool:
    """Whether two canonical food names are the same ingredient for the purpose of
    recognising a habit. Strictly more than half the words in common: "fine rolled
    oat" is "rolled oat", while "peanut butter" is not "butter"."""
    if a == b:
        return True
    ta, tb = _tokens(a), _tokens(b)
    union = ta | tb
    return bool(union) and len(ta & tb) / len(union) > 0.5


def similarity(a: Sequence[str], b: Sequence[str]) -> float:
    """Jaccard similarity of two ingredient lists, with a fuzzy element match: each
    ingredient of `a` may pair with at most one of `b`."""
    if not a or not b:
        return 0.0
    free = list(b)
    matched = 0
    for key in a:
        for i, other in enumerate(free):
            if _items_match(key, other):
                matched += 1
                del free[i]
                break
    return matched / (len(a) + len(b) - matched)


def _parse(stamp: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def _minute_of_day(stamp: datetime) -> int:
    return stamp.hour * 60 + stamp.minute


def _age_days(now: datetime, stamp: datetime) -> float:
    """Days between a meal and `now`, never negative. Compared as wall-clock time
    when only one side carries a zone, which is how every sheet row is written."""
    if (now.tzinfo is None) != (stamp.tzinfo is None):
        now, stamp = now.replace(tzinfo=None), stamp.replace(tzinfo=None)
    return max(0.0, (now - stamp).total_seconds() / 86400)


def _circular_mean_minute(minutes: Iterable[int]) -> int:
    """The typical time of a habit. Circular so that 23:50 and 00:10 average to
    midnight rather than to noon."""
    angles = [m / 1440 * 2 * math.pi for m in minutes]
    x = sum(math.cos(a) for a in angles)
    y = sum(math.sin(a) for a in angles)
    if not angles or (abs(x) < 1e-9 and abs(y) < 1e-9):
        return 0
    return int(round((math.atan2(y, x) % (2 * math.pi)) / (2 * math.pi) * 1440)) % 1440


def _minute_gap(a: int, b: int) -> int:
    gap = abs(a - b) % 1440
    return min(gap, 1440 - gap)


def _food_keys(meal: Dict[str, Any], canonical: Callable[[str], str]) -> List[str]:
    return [canonical(str(i.get("name") or "")) for i in meal.get("items") or []
            if str(i.get("name") or "").strip() and not is_placeholder(i)]


def _signature(meal: Dict[str, Any], canonical: Callable[[str], str]) -> tuple:
    """What makes two versions of a habit the same version: the same foods at the
    same grams. Used to stop "the latest" and "an alternative" being identical."""
    return tuple(sorted(
        (canonical(str(i.get("name") or "")), round(float(i.get("portion_g") or 0)))
        for i in meal.get("items") or [] if not is_placeholder(i)))


def families(meals: Sequence[Dict[str, Any]], *, now: datetime,
             canonical: Callable[[str], str],
             limit: int = 12) -> List[Dict[str, Any]]:
    """The user's habits, best suggestion first.

    `meals` are real meal rows (stubs already removed) with a parseable `datetime`
    and English `items`. Each is compared, newest first, against the LATEST member of
    every habit found so far — anchoring on the latest is what lets a habit drift
    (the banana arrives, the peanut butter leaves) without splitting.

    Ranked by how often and how recently it was eaten, weighted towards habits
    usually eaten around `now`'s time of day. Each result carries its members newest
    first (`meals`), the distinct recent versions (`versions`, latest included), its
    size and its typical time."""
    dated = []
    for meal in meals:
        stamp = _parse(meal.get("datetime"))
        keys = _food_keys(meal, canonical)
        if stamp is not None and keys:
            dated.append((stamp, keys, meal))
    dated.sort(key=lambda entry: entry[0], reverse=True)

    groups: List[Dict[str, Any]] = []
    for stamp, keys, meal in dated:
        best, best_sim = None, 0.0
        for group in groups:
            sim = similarity(keys, group["keys"])
            if sim > best_sim:
                best, best_sim = group, sim
        if best is not None and best_sim >= SIMILARITY:
            best["members"].append((stamp, meal))
        else:
            groups.append({"keys": keys, "members": [(stamp, meal)]})

    now_minute = _minute_of_day(now)
    out: List[Dict[str, Any]] = []
    for group in groups:
        members = group["members"]
        if len(members) < FAMILY_MIN_MEALS:
            continue
        typical = _circular_mean_minute(_minute_of_day(s) for s, _ in members)
        gap = _minute_gap(typical, now_minute)
        affinity = TIME_FLOOR + (1 - TIME_FLOOR) * math.exp(-(gap / TIME_SCALE_MIN) ** 2)
        weight = 0.0
        for stamp, _ in members:
            weight += 0.5 ** (_age_days(now, stamp) / RECENCY_HALF_LIFE_DAYS)

        versions, seen = [], set()
        for _, meal in members:
            sig = _signature(meal, canonical)
            if sig in seen:
                continue
            seen.add(sig)
            versions.append(meal)
            if len(versions) >= MAX_VERSIONS:
                break

        out.append({
            "meals": [m for _, m in members],
            "versions": versions,
            "count": len(members),
            "typical_time": f"{typical // 60:02d}:{typical % 60:02d}",
            "score": round(weight * affinity, 4),
        })
    out.sort(key=lambda f: f["score"], reverse=True)
    return out[:limit]


def ingredients(meals: Sequence[Dict[str, Any]], *,
                canonical: Callable[[str], str],
                limit: int = 200) -> List[Dict[str, Any]]:
    """Every distinct food ever logged, most-eaten first, each with its LATEST
    occurrence as the basis for adding it to a meal (latest, because the most
    recent estimate of a food reflects the most recent product the user buys).

    An item with no weight cannot be rescaled, but it is still offered — a daily
    supplement is exactly the kind of thing one adds to a meal as-is."""
    seen: Dict[str, Dict[str, Any]] = {}
    ordered = sorted(
        ((stamp, meal) for meal in meals
         if (stamp := _parse(meal.get("datetime"))) is not None),
        key=lambda entry: entry[0], reverse=True)
    for stamp, meal in ordered:
        for item in meal.get("items") or []:
            name = str(item.get("name") or "").strip()
            if not name or is_placeholder(item):
                continue
            if not any((_nonneg(item.get(k)) or 0) > 0 for k in MACRO_KEYS) \
                    and not item.get("nutrients"):
                continue
            key = canonical(name)
            entry = seen.get(key)
            if entry is None:
                seen[key] = {"key": key, "item": item, "count": 1,
                             "last": meal.get("datetime")}
            else:
                entry["count"] += 1
    ranked = sorted(seen.values(), key=lambda e: e["count"], reverse=True)
    return ranked[:limit]
