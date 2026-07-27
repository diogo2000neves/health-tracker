"""What a nutritionist would notice in the food log — as facts, not prose.

The coach's first version reasoned only over nutrient totals, so the best it could
ever say was a restatement of the Nutrients tab ("fibre is at 15 g of 28 g"). A
dietitian looking at the same log reads something else entirely: *white rice seven
times, fries four times, fish almost never, vegetables at two of twenty-one meals*.
This module computes that reading.

Everything here is a pure function of the meal rows plus the taxonomy, so the whole
food-level analysis is unit-tested and can be eyeballed via `/coach/patterns` before
a single model call is spent. The model's only job downstream is to turn these
findings into one warm Portuguese sentence each — it never decides *what* is true.

What comes out:

  * `groups` — servings per week, occurrences, days-since-last and the weekly
    reference range, per food group.
  * `foods` — the canonical vocabulary with frequency, typical portion and the slot
    each food usually shows up in.
  * `variety` — distinct foods and distinct vegetables per week, plus how
    concentrated the diet is in its top few foods.
  * `slots` — per meal slot, how often it carries a protein and a plant food, which
    is where "your breakfasts have no fruit" comes from.
  * `findings` — the ranked, typed observations, each carrying the evidence that
    produced it, so nothing the coach says can be unfalsifiable.
  * `swaps` — for a finding, concrete replacements drawn FIRST from foods the user
    already eats. This is the guard against the failure the old critic pass caught
    live: a model inventing "swap your white bread" when no bread was ever logged.

The reference ranges live in `food_taxonomy.GROUP_INFO` and come from mainstream
dietary guidance. A finding is an observation about what was logged against that
guidance — never a diagnosis, and the phrasing rules downstream keep it that way.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import food_taxonomy as tax

# A finding needs at least this much of the window to be worth stating: with three
# logged days, "no fish this week" is a gap in the log, not a gap in the diet.
MIN_DAYS_FOR_FINDING = 5

# How far above / below its reference a group has to sit before it's worth a card.
# 1.3 and 0.7 keep the coach off the "you had 3.1 of 3 servings" hair-trigger.
OVER_RATIO = 1.3
UNDER_RATIO = 0.7

# A food eaten on this share of logged days in a row reads as a routine, not a
# coincidence — the basis of "white rice at lunch four days running".
STREAK_MIN_DAYS = 3

# These non-meals never happened, food-wise (mirrors main.NON_MEALS).
NON_MEALS = {"not food", "analysis failed"}


# -- row parsing ---------------------------------------------------------------
# Deliberately local and stdlib-only, mirroring insights.py: this module stays a
# pure function of the rows it is handed, so importing it is free and it can be
# tested without the sheet, the taxonomy blob or credentials.

def _num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):
        return 0.0
    return out if out > 0 else 0.0


def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict)]
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [i for i in parsed if isinstance(i, dict)] if isinstance(parsed, list) else []


def _is_real_meal(row: Dict[str, Any]) -> bool:
    """A row that actually contributed food. Unlike the nutrient lenses this does
    NOT require calories > 0 — a zero-calorie supplement still belongs in the food
    vocabulary — but it does drop the stubs, which are audit trail, not food."""
    if str(row.get("foods") or "").strip().lower() in NON_MEALS:
        return False
    return bool(_parse_items(row.get("items")))


def _day(row: Dict[str, Any]) -> str:
    return str(row.get("datetime") or "")[:10]


def meal_slot(datetime_str: Any) -> str:
    """breakfast / morning_snack / lunch / afternoon_snack / dinner from the local
    hour (same boundaries as insights._meal_slot, so every screen agrees)."""
    try:
        hour = int(str(datetime_str)[11:13])
    except (ValueError, IndexError):
        hour = 0
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 18 <= hour < 23:
        return "dinner"
    if 15 <= hour < 18:
        return "afternoon_snack"
    return "morning_snack"


SLOT_LABELS = {
    "breakfast": "pequeno-almoço",
    "morning_snack": "lanche da manhã",
    "lunch": "almoço",
    "afternoon_snack": "lanche da tarde",
    "dinner": "jantar",
}


# -- the canonical reading of the log ------------------------------------------

def read_meals(window_meals: Sequence[Dict[str, Any]],
               taxonomy: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The log re-expressed in canonical foods and groups: one entry per meal, with
    its items resolved through the taxonomy. Everything else in this module reads
    this, so a name is canonicalised exactly once."""
    out: List[Dict[str, Any]] = []
    for row in window_meals:
        if not _is_real_meal(row):
            continue
        items = []
        for item in _parse_items(row.get("items")):
            raw_name = str(item.get("name") or "").strip()
            if not raw_name:
                continue
            info = tax.lookup(taxonomy, raw_name)
            grams = _num(item.get("portion_g"))
            items.append({
                "raw": raw_name,
                "food": info["canonical"],
                # Two pt-PT names, for two jobs: `pt` keeps the logged detail
                # ("peito de frango grelhado") for anything quoting one meal, while
                # `pt_food` names the canonical bucket ("peito de frango") that the
                # aggregations below count under. Mixing them would let the coach
                # say "grilled chicken breast, 5 times" about a bucket that also
                # holds the boiled ones.
                "pt": info["pt"],
                "pt_food": info["pt_canonical"],
                "group": info["group"],
                "fried": info["fried"],
                "grams": grams,
                "calories": _num(item.get("calories")),
                "servings": tax.servings(info["group"], grams),
                "nutrients": item.get("nutrients") if isinstance(
                    item.get("nutrients"), dict) else {},
            })
        if not items:
            continue
        out.append({
            "datetime": str(row.get("datetime") or ""),
            "date": _day(row),
            "slot": meal_slot(row.get("datetime")),
            "calories": _num(row.get("calories")),
            "protein_g": _num(row.get("protein_g")),
            "items": items,
            "groups": sorted({i["group"] for i in items}),
            # The user's own words about this meal ("Comi um menu médio Big Tasty do
            # McDonalds"). Carried through because it is often the only place the
            # context lives — the items say "burger, fries, iced tea" and the note
            # says where they came from and why.
            "note": " ".join(str(row.get("note") or "").split())[:400],
        })
    out.sort(key=lambda m: m["datetime"])
    return out


def logged_days(meals: Sequence[Dict[str, Any]]) -> List[str]:
    return sorted({m["date"] for m in meals if m["date"]})


def group_stats(meals: Sequence[Dict[str, Any]], *, window_days: int,
                ref_day: str) -> Dict[str, Dict[str, Any]]:
    """Per group: servings and occurrences over the window, both as a raw count and
    normalised to a week, plus the last day it appeared.

    Normalising to a week (rather than reporting a 28-day count) is what lets the
    numbers be compared with the reference ranges directly, and keeps the arithmetic
    out of the model's hands.
    """
    days = logged_days(meals)
    day_count = max(len(days), 1)
    pt = pt_index(meals)
    stats: Dict[str, Dict[str, Any]] = {}
    for meal in meals:
        for item in meal["items"]:
            group = item["group"]
            rec = stats.setdefault(group, {
                "group": group, "label": tax.label(group), "servings": 0.0,
                "occurrences": 0, "grams": 0.0, "calories": 0.0,
                "days": set(), "last": "", "foods": {},
            })
            rec["servings"] += item["servings"]
            rec["occurrences"] += 1
            rec["grams"] += item["grams"]
            rec["calories"] += item["calories"]
            rec["days"].add(meal["date"])
            rec["last"] = max(rec["last"], meal["date"])
            rec["foods"][item["food"]] = rec["foods"].get(item["food"], 0) + 1

    for group, rec in stats.items():
        info = tax.GROUP_INFO.get(group, tax.GROUP_INFO["other"])
        per_week = 7.0 / day_count
        rec["days_logged"] = len(rec.pop("days"))
        rec["servings"] = round(rec["servings"], 2)
        rec["servings_per_week"] = round(rec["servings"] * per_week, 1)
        rec["occurrences_per_week"] = round(rec["occurrences"] * per_week, 1)
        rec["grams"] = round(rec["grams"])
        rec["calories"] = round(rec["calories"])
        rec["days_since_last"] = _days_between(rec["last"], ref_day)
        rec["posture"] = info.get("posture", "neutral")
        rec["week_min"] = info.get("week_min")
        rec["week_max"] = info.get("week_max")
        rec["top_foods"] = [f for f, _ in sorted(rec.pop("foods").items(),
                                                key=lambda kv: kv[1], reverse=True)[:4]]
        rec["top_foods_pt"] = [pt.get(f, f) for f in rec["top_foods"]]
    # Groups with a `more` posture that never appeared at all still matter — "no
    # fish in the window" is the single most useful thing here, and it lives in the
    # absence of a row. Materialise them at zero.
    for group, info in tax.GROUP_INFO.items():
        if group in stats or info.get("posture") != "more":
            continue
        stats[group] = {
            "group": group, "label": tax.label(group), "servings": 0.0,
            "servings_per_week": 0.0, "occurrences": 0, "occurrences_per_week": 0.0,
            "grams": 0, "calories": 0, "days_logged": 0, "last": "",
            "days_since_last": None, "posture": "more",
            "week_min": info.get("week_min"), "week_max": info.get("week_max"),
            "top_foods": [], "top_foods_pt": [],
        }
    return stats


def pt_index(meals: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """canonical English food -> its pt-PT name.

    Every aggregation below keys on the canonical name — that is the entire point of
    the taxonomy — but everything they FEED is Portuguese prose. Rather than thread
    the display name through each one, they look it up here and carry it alongside
    their key.
    """
    out: Dict[str, str] = {}
    for meal in meals:
        for item in meal["items"]:
            out.setdefault(item["food"], item["pt_food"])
    return out


def food_stats(meals: Sequence[Dict[str, Any]], *, ref_day: str
               ) -> List[Dict[str, Any]]:
    """The canonical food vocabulary: how often, how much, when, and in what group.
    Ordered by frequency — the top of this list is what the user actually eats, and
    it is the pool every suggestion has to draw from first."""
    agg: Dict[str, Dict[str, Any]] = {}
    for meal in meals:
        for item in meal["items"]:
            rec = agg.setdefault(item["food"], {
                "food": item["food"], "pt": item["pt_food"],
                "group": item["group"], "times": 0,
                "portions": [], "slots": {}, "last": "", "raw_names": set(),
                "pt_names": set(), "fried_times": 0,
            })
            rec["times"] += 1
            if item["grams"] > 0:
                rec["portions"].append(item["grams"])
            rec["slots"][meal["slot"]] = rec["slots"].get(meal["slot"], 0) + 1
            rec["last"] = max(rec["last"], meal["date"])
            rec["raw_names"].add(item["raw"])
            rec["pt_names"].add(item["pt"])
            if item["fried"]:
                rec["fried_times"] += 1
    out = []
    for rec in agg.values():
        portions = rec.pop("portions")
        rec["median_portion_g"] = round(statistics.median(portions)) if portions else 0
        slots = rec.pop("slots")
        rec["top_slot"] = max(slots, key=slots.get) if slots else "lunch"
        rec["slot_label"] = SLOT_LABELS.get(rec["top_slot"], rec["top_slot"])
        rec["days_since_last"] = _days_between(rec["last"], ref_day)
        rec["raw_names"] = sorted(rec["raw_names"])[:4]
        # Both spellings are kept so a model answer can be matched back to this food
        # however it phrased it — the coach is prompted in Portuguese and will say
        # "peito de frango", but the log's own word may be either.
        rec["pt_names"] = sorted(rec["pt_names"])[:4]
        out.append(rec)
    out.sort(key=lambda r: (-r["times"], r["food"]))
    return out


def variety(meals: Sequence[Dict[str, Any]], foods: Sequence[Dict[str, Any]]
            ) -> Dict[str, Any]:
    """How wide the diet is, and how concentrated.

    `top_share` is the fraction of all food occurrences taken by the five most
    frequent foods — the honest version of "you eat the same things every day",
    and a number the coach can quote.
    """
    days = max(len(logged_days(meals)), 1)
    total = sum(f["times"] for f in foods) or 1
    veg = [f for f in foods if f["group"] == "vegetable"]
    fruit = [f for f in foods if f["group"] == "fruit"]
    return {
        "distinct_foods": len(foods),
        "distinct_foods_per_week": round(len(foods) * 7.0 / days, 1),
        "distinct_vegetables": len(veg),
        "distinct_fruits": len(fruit),
        "top_share": round(sum(f["times"] for f in foods[:5]) / total, 2),
        "top_foods": [f["food"] for f in foods[:5]],
        "top_foods_pt": [f.get("pt") or f["food"] for f in foods[:5]],
        "days_logged": days,
    }


def slot_stats(meals: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per meal slot: how many were logged, the typical hour, and how often the
    plate carried a protein food and a plant food. This is where composition advice
    comes from — "your dinners are protein and starch, no vegetable" is a fact about
    these counts, not an impression."""
    stats: Dict[str, Dict[str, Any]] = {}
    for meal in meals:
        rec = stats.setdefault(meal["slot"], {
            "slot": meal["slot"], "label": SLOT_LABELS.get(meal["slot"], meal["slot"]),
            "meals": 0, "with_protein": 0, "with_plant": 0, "with_vegetable": 0,
            "hours": [], "calories": [],
        })
        rec["meals"] += 1
        groups = set(meal["groups"])
        if groups & set(tax.PROTEIN_GROUPS):
            rec["with_protein"] += 1
        if groups & set(tax.PLANT_GROUPS):
            rec["with_plant"] += 1
        if "vegetable" in groups:
            rec["with_vegetable"] += 1
        try:
            rec["hours"].append(int(meal["datetime"][11:13]))
        except (ValueError, IndexError):
            pass
        if meal["calories"] > 0:
            rec["calories"].append(meal["calories"])
    for rec in stats.values():
        hours = rec.pop("hours")
        cals = rec.pop("calories")
        rec["typical_hour"] = round(statistics.mean(hours)) if hours else None
        rec["median_calories"] = round(statistics.median(cals)) if cals else 0
        rec["protein_pct"] = round(rec["with_protein"] / rec["meals"], 2)
        rec["plant_pct"] = round(rec["with_plant"] / rec["meals"], 2)
        rec["vegetable_pct"] = round(rec["with_vegetable"] / rec["meals"], 2)
    return stats


def streaks(meals: Sequence[Dict[str, Any]], *, ref_day: str
            ) -> List[Dict[str, Any]]:
    """Foods eaten in the same slot on consecutive logged days, longest first.

    Consecutive *logged* days, not calendar days: a gap in the log is not evidence
    that the routine broke.
    """
    days = logged_days(meals)
    index = {day: i for i, day in enumerate(days)}
    pt = pt_index(meals)
    seen: Dict[Tuple[str, str], List[int]] = {}
    for meal in meals:
        for item in meal["items"]:
            seen.setdefault((item["food"], meal["slot"]), []).append(index[meal["date"]])

    out = []
    for (food, slot), positions in seen.items():
        ordered = sorted(set(positions))
        best = run = 1
        best_end = run_end = ordered[0]
        for prev, cur in zip(ordered, ordered[1:]):
            if cur == prev + 1:
                run += 1
                run_end = cur
            else:
                run, run_end = 1, cur
            if run > best:
                best, best_end = run, run_end
        if best >= STREAK_MIN_DAYS:
            out.append({
                "food": food, "pt": pt.get(food, food), "slot": slot,
                "slot_label": SLOT_LABELS.get(slot, slot),
                "days": best,
                "ended": days[best_end],
                "current": best_end == len(days) - 1,
            })
    out.sort(key=lambda s: (-s["days"], s["food"]))
    return out


def nutrient_drivers(meals: Sequence[Dict[str, Any]], key: str, *, top: int = 3
                     ) -> List[Dict[str, Any]]:
    """The canonical foods supplying a nutrient across the window, biggest share
    first. Same idea as insights._attribution, but aggregated by canonical food, so
    "cod" doesn't split into three rows and lose to a food that only looks bigger."""
    sums: Dict[str, float] = {}
    total = 0.0
    for meal in meals:
        for item in meal["items"]:
            amount = _num(item["nutrients"].get(key))
            if amount <= 0:
                continue
            sums[item["food"]] = sums.get(item["food"], 0.0) + amount
            total += amount
    if total <= 0:
        return []
    pt = pt_index(meals)
    ranked = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [{"food": food, "pt": pt.get(food, food),
             "pct": round(100 * amount / total)}
            for food, amount in ranked]


# -- findings ------------------------------------------------------------------

def _finding(kind: str, group: Optional[str], severity: float, *, headline: str,
             evidence: Dict[str, Any], foods: Sequence[str] = ()) -> Dict[str, Any]:
    """One observation. `headline` is a plain factual English/pt-PT-neutral summary
    for the prompt — the model rewrites it warmly, but the fact and the evidence
    travel together so a critic can check the sentence against them."""
    return {
        "id": f"{kind}:{group or 'none'}",
        "kind": kind,
        "group": group,
        "severity": round(severity, 2),
        "headline": headline,
        "evidence": evidence,
        "foods": list(foods),
    }


def build_findings(*, groups: Dict[str, Dict[str, Any]],
                   slots: Dict[str, Dict[str, Any]],
                   variety_stats: Dict[str, Any],
                   food_streaks: Sequence[Dict[str, Any]],
                   days_logged: int) -> List[Dict[str, Any]]:
    """The ranked observations. Severity is a blunt 0..1 score used only to order
    them, so the feed leads with the thing most worth one sentence.

    Nothing here fires below `MIN_DAYS_FOR_FINDING` logged days: with a thin log the
    honest answer is "not enough yet", and inventing urgency from three days is
    exactly the failure mode that makes a coach untrustworthy.
    """
    if days_logged < MIN_DAYS_FOR_FINDING:
        return []

    out: List[Dict[str, Any]] = []

    for group, rec in groups.items():
        per_week = rec.get("servings_per_week") or 0.0
        week_max = rec.get("week_max")
        week_min = rec.get("week_min")

        if week_max and per_week > week_max * OVER_RATIO:
            over = per_week / week_max
            out.append(_finding(
                "group_over", group, min(0.95, 0.45 + 0.25 * (over - 1)),
                headline=(f"{rec['label']}: {per_week} doses/semana, acima da "
                          f"referência de {week_max}"),
                evidence={"servings_per_week": per_week, "reference_max": week_max,
                          "occurrences": rec.get("occurrences"),
                          "top_foods": rec.get("top_foods_pt", [])},
                foods=rec.get("top_foods_pt", [])))

        if week_min and per_week < week_min * UNDER_RATIO:
            short = 1 - (per_week / week_min)
            # An absent `more` group is the strongest signal in the whole module:
            # "no fish at all in the window" beats any partial shortfall.
            severity = min(0.95, 0.4 + 0.5 * short)
            out.append(_finding(
                "group_under", group, severity,
                headline=(f"{rec['label']}: {per_week} doses/semana, abaixo da "
                          f"referência de {week_min}"
                          + ("; nada registado na janela" if per_week == 0 else "")),
                evidence={"servings_per_week": per_week, "reference_min": week_min,
                          "days_since_last": rec.get("days_since_last"),
                          "top_foods": rec.get("top_foods_pt", [])},
                foods=rec.get("top_foods_pt", [])))

    # Refined vs whole grains: a ratio says more than either count alone.
    refined = (groups.get("refined_grain") or {}).get("servings_per_week") or 0.0
    whole = (groups.get("whole_grain") or {}).get("servings_per_week") or 0.0
    if refined + whole >= 4 and refined > 0 and whole / (refined + whole) < 0.34:
        out.append(_finding(
            "refined_share", "refined_grain",
            min(0.9, 0.4 + 0.5 * (refined / (refined + whole) - 0.66)),
            headline=(f"{round(100 * refined / (refined + whole))}% dos cereais da "
                      f"semana são refinados"),
            evidence={"refined_per_week": refined, "whole_per_week": whole,
                      "whole_share": round(whole / (refined + whole), 2),
                      "top_foods": (groups.get("refined_grain") or {}).get("top_foods_pt", [])},
            foods=(groups.get("refined_grain") or {}).get("top_foods_pt", [])))

    # Composition: a slot that usually arrives without a plant food.
    for slot, rec in slots.items():
        if rec["meals"] < 4 or slot in ("morning_snack", "afternoon_snack"):
            continue
        if rec["plant_pct"] < 0.5:
            out.append(_finding(
                "slot_no_plant", None, 0.35 + 0.4 * (0.5 - rec["plant_pct"]),
                headline=(f"{rec['label']}: só {round(100 * rec['plant_pct'])}% "
                          f"das refeições levam legumes, fruta ou leguminosas"),
                evidence={"slot": slot, "meals": rec["meals"],
                          "with_plant": rec["with_plant"],
                          "plant_pct": rec["plant_pct"]}))
        if rec["protein_pct"] < 0.6:
            out.append(_finding(
                "slot_no_protein", None, 0.3 + 0.3 * (0.6 - rec["protein_pct"]),
                headline=(f"{rec['label']}: só {round(100 * rec['protein_pct'])}% "
                          f"das refeições levam uma fonte de proteína"),
                evidence={"slot": slot, "meals": rec["meals"],
                          "with_protein": rec["with_protein"],
                          "protein_pct": rec["protein_pct"]}))

    # Repetition: same handful of foods over and over.
    if variety_stats["top_share"] >= 0.45 and variety_stats["distinct_foods"] >= 5:
        out.append(_finding(
            "repetition", None, 0.3 + 0.4 * (variety_stats["top_share"] - 0.45),
            headline=(f"{round(100 * variety_stats['top_share'])}% do que comes vem "
                      f"dos mesmos 5 alimentos"),
            evidence={"top_share": variety_stats["top_share"],
                      "top_foods": variety_stats["top_foods_pt"],
                      "distinct_foods": variety_stats["distinct_foods"]},
            foods=variety_stats["top_foods_pt"]))

    if variety_stats["distinct_vegetables"] <= 3:
        out.append(_finding(
            "veg_variety", "vegetable", 0.45,
            headline=(f"apenas {variety_stats['distinct_vegetables']} legumes "
                      f"diferentes em {variety_stats['days_logged']} dias"),
            evidence={"distinct_vegetables": variety_stats["distinct_vegetables"],
                      "days_logged": variety_stats["days_logged"]}))

    # A live routine is the most actionable thing the coach can name, so it ranks
    # above the statistical observations when it's still running.
    for streak in food_streaks[:2]:
        out.append(_finding(
            "streak", None, 0.5 if streak["current"] else 0.3,
            headline=(f"{streak.get('pt') or streak['food']} ao "
                      f"{streak['slot_label']} {streak['days']} dias seguidos"),
            evidence=dict(streak), foods=[streak.get("pt") or streak["food"]]))

    out.sort(key=lambda f: -f["severity"])
    return out


# -- swaps ---------------------------------------------------------------------
# Better options *within reach*: a food the user has already logged beats a
# perfectly-chosen food they have never eaten. Only when the log has nothing to
# offer do we fall back to the small curated list below — and then the suggestion is
# marked `new`, so the coach can say so out loud instead of pretending it's a habit.

_BETTER_GROUPS: Dict[str, Tuple[str, ...]] = {
    "red_meat": ("fish_white", "fish_oily", "poultry", "legume"),
    "processed_meat": ("fish_white", "fish_oily", "poultry", "egg", "legume"),
    "refined_grain": ("whole_grain", "legume", "potato"),
    "fried_potato": ("potato", "vegetable", "legume"),
    "sweet": ("fruit", "dairy_plain", "nut_seed"),
    "dairy_sweet": ("fruit", "dairy_plain", "nut_seed"),
    "savory_snack": ("nut_seed", "fruit", "vegetable"),
    "sugary_drink": ("drink_free", "fruit"),
    "alcohol": ("drink_free",),
    "fat_sat": ("fat_healthy", "nut_seed"),
}

# Which groups to reach for depends on the meal as well as the problem. Cutting the
# breakfast ham means an egg or fresh cheese, not a fillet of cod — the generic
# ranking is nutritionally right and practically absurd at 8 a.m.
_BETTER_GROUPS_BY_SLOT: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "breakfast": {
        "processed_meat": ("egg", "dairy_plain", "cheese", "nut_seed"),
        "refined_grain": ("whole_grain", "fruit", "dairy_plain"),
        "sweet": ("fruit", "dairy_plain", "nut_seed"),
    },
    "morning_snack": {
        "sweet": ("fruit", "nut_seed", "dairy_plain"),
        "savory_snack": ("fruit", "nut_seed"),
    },
    "afternoon_snack": {
        "sweet": ("fruit", "nut_seed", "dairy_plain"),
        "savory_snack": ("fruit", "nut_seed"),
        "processed_meat": ("egg", "dairy_plain", "nut_seed"),
    },
}

# Staples that belong to a particular meal, used when the log has nothing to offer
# in the group we want at that time of day.
_SLOT_STAPLES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "breakfast": {
        "egg": ("ovo cozido", "ovos mexidos"),
        "dairy_plain": ("iogurte natural", "skyr"),
        "cheese": ("queijo fresco",),
        "nut_seed": ("manteiga de amendoim", "amêndoas"),
        "fish_oily": ("atum",),
        "whole_grain": ("aveia", "pão integral"),
        "fruit": ("banana", "maçã"),
    },
    "afternoon_snack": {
        "dairy_plain": ("iogurte natural", "skyr"),
        "nut_seed": ("amêndoas", "nozes"),
        "fruit": ("maçã", "pera"),
        "egg": ("ovo cozido",),
    },
}

# Common, cheap, Portuguese-supermarket staples per group — the fallback when the
# log has nothing in a group. Deliberately short and boring: a suggestion the user
# won't buy is worse than no suggestion.
_STAPLES: Dict[str, Tuple[str, ...]] = {
    "fish_oily": ("sardinha", "cavala", "salmão"),
    "fish_white": ("bacalhau", "pescada", "dourada"),
    "legume": ("feijão preto", "grão-de-bico", "lentilhas"),
    "whole_grain": ("aveia", "arroz integral", "pão integral"),
    "vegetable": ("brócolos", "espinafres", "courgette"),
    "fruit": ("maçã", "pera", "banana"),
    "nut_seed": ("amêndoas", "nozes"),
    "dairy_plain": ("iogurte natural", "skyr"),
    "potato": ("batata cozida", "batata-doce"),
    "poultry": ("peito de frango", "peru"),
    "drink_free": ("água", "chá", "café sem açúcar"),
    "egg": ("ovo cozido",),
}


def swap_candidates(finding: Dict[str, Any], foods: Sequence[Dict[str, Any]], *,
                    limit: int = 3) -> Dict[str, Any]:
    """Concrete replacements for a finding: what to move away from (only ever a food
    that appears in the log) and what to move toward.

    Replacements are ranked by whether they fit the MEAL the original food is actually
    eaten at. Ham eaten in a breakfast sandwich must not be answered with codfish: it
    is nutritionally sound and practically useless, which is worse than saying nothing.
    So a candidate the user eats at the same slot outranks one they eat at another
    slot, which outranks a staple they have never logged.

    Returns `{"from": [...], "to": [...]}` where every `to` entry carries the slot it
    belongs to and whether it is already part of the diet. The model picks and phrases;
    it cannot invent either side.
    """
    group = finding.get("group")
    from_foods: List[Dict[str, Any]] = []
    if group:
        for food in foods:
            if food["group"] == group:
                from_foods.append({"food": food["food"],
                                   "pt": food.get("pt") or food["food"],
                                   "times": food["times"],
                                   "median_portion_g": food["median_portion_g"],
                                   "slot": food["top_slot"],
                                   "slot_label": food["slot_label"]})
            if len(from_foods) >= limit:
                break

    # The meal the thing we're replacing actually belongs to.
    target_slot = from_foods[0]["slot"] if from_foods else finding.get(
        "evidence", {}).get("slot")

    wanted: Tuple[str, ...] = ()
    if finding["kind"] in ("group_over", "refined_share", "streak") and group:
        by_slot = _BETTER_GROUPS_BY_SLOT.get(target_slot or "", {})
        wanted = by_slot.get(group) or _BETTER_GROUPS.get(group, ())
    elif finding["kind"] == "group_under" and group:
        wanted = (group,)
    elif finding["kind"] in ("slot_no_plant", "veg_variety"):
        wanted = ("vegetable", "fruit", "legume")
    elif finding["kind"] == "slot_no_protein":
        wanted = ("egg", "dairy_plain", "fish_white", "poultry", "legume")
    elif finding["kind"] == "repetition":
        wanted = ("vegetable", "legume", "fish_white", "fruit")

    to_foods: List[Dict[str, Any]] = []
    for target in wanted:
        eaten = [f for f in foods if f["group"] == target]
        # Foods eaten at the same meal as the one being replaced come first.
        eaten.sort(key=lambda f: (f["top_slot"] != target_slot, -f["times"]))
        for food in eaten[:2]:
            to_foods.append({"food": food["food"],
                             "pt": food.get("pt") or food["food"],
                             "group": target,
                             "median_portion_g": food["median_portion_g"],
                             "times": food["times"], "new": False,
                             "slot": food["top_slot"],
                             "slot_label": food["slot_label"],
                             "fits_the_meal": food["top_slot"] == target_slot})
        if not eaten:
            slot_staples = _SLOT_STAPLES.get(target_slot or "", {}).get(target)
            # `_STAPLES` is written in pt-PT already — these are foods the user has
            # never logged, so there is no English canonical to carry.
            for staple in (slot_staples or _STAPLES.get(target, ()))[:2]:
                to_foods.append({"food": staple, "pt": staple, "group": target,
                                 "median_portion_g": tax.GROUP_INFO.get(
                                     target, {}).get("serving_g", 100),
                                 "times": 0, "new": True, "slot": None,
                                 "slot_label": "", "fits_the_meal": None})
        if len(to_foods) >= limit + 2:
            break

    # A replacement that fits the meal outranks one that doesn't, whatever group it
    # came from — the model reads this list top-down.
    to_foods.sort(key=lambda t: (t.get("fits_the_meal") is not True,
                                 t.get("new") is True))
    return {"from": from_foods, "to": to_foods[: limit + 1],
            "replacing_at": target_slot,
            "replacing_at_label": SLOT_LABELS.get(target_slot or "", "")}


# -- the whole profile ---------------------------------------------------------

def build_food_profile(window_meals: Sequence[Dict[str, Any]], *,
                       taxonomy: Optional[Dict[str, Any]],
                       window_days: int, ref_day: str) -> Dict[str, Any]:
    """The complete food-level reading of the window — the object the card generator
    and the chat both reason over."""
    meals = read_meals(window_meals, taxonomy)
    days = logged_days(meals)
    groups = group_stats(meals, window_days=window_days, ref_day=ref_day)
    foods = food_stats(meals, ref_day=ref_day)
    variety_stats = variety(meals, foods)
    slots = slot_stats(meals)
    food_streaks = streaks(meals, ref_day=ref_day) if meals else []
    findings = build_findings(groups=groups, slots=slots,
                              variety_stats=variety_stats,
                              food_streaks=food_streaks,
                              days_logged=len(days))
    return {
        "ref_day": ref_day,
        "window_days": window_days,
        "days_logged": len(days),
        "meals_logged": len(meals),
        "groups": groups,
        "foods": foods,
        "variety": variety_stats,
        "slots": slots,
        "streaks": food_streaks,
        "findings": findings,
        "swaps": {f["id"]: swap_candidates(f, foods) for f in findings[:6]},
    }


def _days_between(then: str, ref_day: str) -> Optional[int]:
    if not then:
        return None
    try:
        a = date.fromisoformat(then)
        b = date.fromisoformat(ref_day)
    except ValueError:
        return None
    return (b - a).days
