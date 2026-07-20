#!/usr/bin/env python3
"""Shared nutrient schema + normalization for the audit pipeline.

Every stage (independent estimate -> adjudication -> FDC grounding -> the eval
harness) must produce and consume the EXACT item/nutrient shape the cloud ingest
service writes, because the daily roll-up sums `meals.items` straight into
`daily_summary`. That shape lived, duplicated, inside audit.py; it now lives here
once so a single edit keeps ingest, the audit, and the eval in lockstep.

The key set is ported verbatim from backend/ingest/main.py (NUTRIENT_KEYS). If the
backend adds a nutrient column, add it here too and the whole pipeline follows.
"""
from __future__ import annotations

from typing import Any, Dict, List

# -- the full per-ingredient nutrient key set (ported verbatim from ingest) -----
NUTRIENTS_G = [
    "fiber_g", "sugar_g", "added_sugar_g", "saturated_fat_g",
    "monounsaturated_fat_g", "polyunsaturated_fat_g", "trans_fat_g",
    "omega3_g", "omega6_g",
]
NUTRIENTS_MG = [
    "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg", "magnesium_mg",
    "zinc_mg", "phosphorus_mg", "copper_mg", "manganese_mg", "chloride_mg",
    "cholesterol_mg", "choline_mg", "vitamin_c_mg", "vitamin_e_mg",
    "vitamin_b1_mg", "vitamin_b2_mg", "vitamin_b3_mg", "vitamin_b5_mg",
    "vitamin_b6_mg",
]
NUTRIENTS_UG = [
    "vitamin_a_ug", "vitamin_d_ug", "vitamin_k_ug", "vitamin_b12_ug",
    "folate_ug", "biotin_ug", "selenium_ug", "iodine_ug",
]
NUTRIENT_KEYS = NUTRIENTS_G + NUTRIENTS_MG + NUTRIENTS_UG

# The fat-composition sub-group. These are the one place where "how much" (the
# visible total fat, a perception task the vision model is good at) and "what kind"
# (the saturated/mono/poly/omega split, a composition-table fact) genuinely couple:
# the grounding step keeps the split's SHAPE from the food database but rescales it
# to the vision total fat, so sat+mono+poly always tracks the fat actually on the
# plate rather than the database's generic cut. (trans/omega are minor and included
# for completeness of the rescale.)
FAT_COMPOSITION_KEYS = [
    "saturated_fat_g", "monounsaturated_fat_g", "polyunsaturated_fat_g",
    "trans_fat_g", "omega3_g", "omega6_g",
]

# Macro columns re-summed onto the flat meals row (micros live only inside items).
MACRO_KEYS = ["calories", "protein_g", "carbs_g", "fat_g"]

# Rows excluded from all totals (kept in sync with the backend's NON_MEALS).
NON_MEALS = {"not food", "analysis failed"}


def _round_num(value: Any, digits: int = 1) -> float:
    try:
        return max(0.0, round(float(value), digits))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _round_nutrient(key: str, value: float) -> float:
    """g to 2dp, mg/ug to 1dp — the same precision ingest stores."""
    return round(float(value), 2 if key.endswith("_g") else 1)


def normalize_nutrients(raw: Any) -> Dict[str, float]:
    """Keep known, positive nutrient keys, rounded to ingest's precision.
    Unknown keys and zeros/traces are dropped — same rule as ingest."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for key in NUTRIENT_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            try:
                out[key] = _round_nutrient(key, value)
            except OverflowError:
                continue
    return out


def normalize_items(raw: Any) -> List[Dict[str, Any]]:
    """Coerce a model's item list into clean {name, portion_g, macros,
    cooking_method?, nutrients?} dicts — the exact shape ingest stores."""
    items: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:120]
        if not name:
            continue
        item: Dict[str, Any] = {
            "name": name,
            "portion_g": _round_num(entry.get("portion_g")),
            "calories": _round_num(entry.get("calories")),
            "protein_g": _round_num(entry.get("protein_g")),
            "carbs_g": _round_num(entry.get("carbs_g")),
            "fat_g": _round_num(entry.get("fat_g")),
        }
        method = str(entry.get("cooking_method", "")).strip()[:40]
        if method:
            item["cooking_method"] = method
        nutrients = normalize_nutrients(entry.get("nutrients"))
        if nutrients:
            item["nutrients"] = nutrients
        items.append(item)
    return items


def meal_totals(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Row-level macro totals summed over items (never taken from a model)."""
    def total(key: str) -> float:
        return round(sum(i.get(key, 0.0) for i in items), 1)
    return {
        "foods": ", ".join(i["name"] for i in items) if items else "not food",
        "portion_g": total("portion_g"),
        "calories": total("calories"),
        "protein_g": total("protein_g"),
        "carbs_g": total("carbs_g"),
        "fat_g": total("fat_g"),
    }


def nutrient_key_count(items: List[Dict[str, Any]]) -> int:
    """Total populated nutrient keys across all items — the completeness signal
    the review log reports (Gemini's sparse ~6-9 vs a grounded ~30+)."""
    return sum(len(i.get("nutrients", {})) for i in items if isinstance(i, dict))
