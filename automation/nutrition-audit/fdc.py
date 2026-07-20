#!/usr/bin/env python3
"""USDA FoodData Central (FDC) client — the authoritative source for Layer B.

The thesis of the whole rebuild: identifying the food and weighing it is a VISION
problem (the models are good at it and disagree usefully); turning "150 g of roasted
chicken thigh" into 30-odd micronutrient numbers is a KNOWLEDGE-LOOKUP problem, and
a model reciting those from memory is noisy and inconsistent run-to-run. FDC is a
government food-composition database: for a matched food it gives measured per-100 g
values that are accurate, deterministic, and — crucially — comparable across days.

This module does two things: fetch (search + food detail, aggressively cached to
disk so a repeated food costs nothing) and MAP FDC's ~150 raw nutrient fields onto
our 36 keys. The mapping was verified against live API responses:
  * micronutrients map by FDC "nutrient number" (a stable INFOODS tagname), NOT by
    name, so wording changes don't break us;
  * omega-3 / omega-6 have no single FDC field — they are SUMMED from the individual
    fatty acids whose name carries an "n-3" / "n-6" tag (ALA/EPA/DHA, LA/GLA/AA...),
    deliberately excluding the undifferentiated "18:2"/"18:3" totals so nothing is
    double-counted;
  * where FDC ships the same nutrient in several forms we pick the one matching our
    unit/definition: Vitamin A as RAE µg (320, not the IU 318), Vitamin D as µg
    (328, not IU 324), folate as DFE (435) falling back to total (417).
Nutrients FDC genuinely lacks for a food (added sugar, chloride, iodine, biotin are
often blank even in Foundation foods) are simply absent from the returned map; the
grounding step keeps the model's estimate for those keys.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("nutrition-audit")

# DEMO_KEY works out of the box but is rate-limited to ~30 requests/hour. Get a
# free key (instant, no billing) at https://fdc.nal.usda.gov/api-key-signup.html
# and export FDC_API_KEY to lift the limit to ~1000/hour.
API_KEY = os.environ.get("FDC_API_KEY", "DEMO_KEY")
BASE = "https://api.nal.usda.gov/fdc/v1"
HTTP_TIMEOUT_S = int(os.environ.get("FDC_HTTP_TIMEOUT_S", "25"))

# Whole-food data types carry the richest micronutrient panels; Branded is a last
# resort (label data, sparse micros) used only when nothing whole matches.
WHOLE_FOOD_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]

_CACHE_PATH = Path(__file__).resolve().parent / "logs" / "fdc_cache.json"
_cache: Optional[Dict[str, Dict[str, Any]]] = None


class FdcRateLimited(RuntimeError):
    """FDC returned 429 — caller should stop hitting the API this run and fall
    back to model-estimated nutrients."""


# -- nutrient number -> our key (verified against live /food responses) --------
# Simple 1:1 maps. Units already match our key suffixes (mg/µg/g) for every entry
# here, so no unit conversion is needed — we map by number and take the amount.
_BY_NUMBER: Dict[str, str] = {
    "291": "fiber_g", "269": "sugar_g", "539": "added_sugar_g",
    "606": "saturated_fat_g", "645": "monounsaturated_fat_g",
    "646": "polyunsaturated_fat_g", "605": "trans_fat_g",
    "307": "sodium_mg", "306": "potassium_mg", "301": "calcium_mg",
    "303": "iron_mg", "304": "magnesium_mg", "309": "zinc_mg",
    "305": "phosphorus_mg", "312": "copper_mg", "315": "manganese_mg",
    "1088": "chloride_mg", "601": "cholesterol_mg", "421": "choline_mg",
    "401": "vitamin_c_mg", "323": "vitamin_e_mg", "404": "vitamin_b1_mg",
    "405": "vitamin_b2_mg", "406": "vitamin_b3_mg", "410": "vitamin_b5_mg",
    "415": "vitamin_b6_mg", "320": "vitamin_a_ug", "328": "vitamin_d_ug",
    "418": "vitamin_b12_ug", "416": "biotin_ug", "317": "selenium_ug",
    "314": "iodine_ug",
}
# Macros, extracted for QA only (the pipeline keeps the vision macro estimate; FDC
# macros are used to flag gross disagreement, not to overwrite).
_MACRO_BY_NUMBER = {"208": "calories", "203": "protein_g", "204": "fat_g",
                    "205": "carbs_g"}


def _load_cache() -> Dict[str, Dict[str, Any]]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            _cache = {}
        _cache.setdefault("search", {})
        _cache.setdefault("food", {})
    return _cache


def flush_cache() -> None:
    if _cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False))
    except OSError as exc:
        log.warning("  fdc: could not persist cache: %s", exc)


def _get(url: str) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise FdcRateLimited("FDC 429 (rate limit)") from exc
        raise


def _post(url: str, body: Dict[str, Any]) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise FdcRateLimited("FDC 429 (rate limit)") from exc
        raise


def search(query: str, *, page_size: int = 6,
           include_branded: bool = False) -> List[Dict[str, Any]]:
    """Return candidate foods [{fdcId, dataType, description}] for a free-text
    query, whole-food types first. Cached by (query, include_branded)."""
    query = (query or "").strip().lower()
    if not query:
        return []
    cache = _load_cache()
    ckey = f"{query}|{int(include_branded)}|{page_size}"
    if ckey in cache["search"]:
        return cache["search"][ckey]
    types = WHOLE_FOOD_TYPES + (["Branded"] if include_branded else [])
    data = _post(f"{BASE}/foods/search?api_key={API_KEY}",
                 {"query": query, "pageSize": page_size, "dataType": types})
    out = [{"fdcId": f["fdcId"], "dataType": f.get("dataType"),
            "description": f.get("description", "")}
           for f in data.get("foods", []) if f.get("fdcId")]
    cache["search"][ckey] = out
    return out


def get_food(fdc_id: int) -> Dict[str, Any]:
    """Fetch and map one food to per-100 g values. Returns
    {fdcId, description, dataType, nutrients:{key:per100g}, macros:{...}}.
    The mapped result (small) is cached, never the raw ~40 KB detail."""
    cache = _load_cache()
    skey = str(fdc_id)
    if skey in cache["food"]:
        return cache["food"][skey]
    raw = _get(f"{BASE}/food/{fdc_id}?api_key={API_KEY}")
    mapped = _map_food(raw)
    cache["food"][skey] = mapped
    return mapped


def _map_food(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a raw /food detail into our per-100 g nutrient + macro maps."""
    nutrients: Dict[str, float] = {}
    macros: Dict[str, float] = {}
    omega3 = 0.0
    omega6 = 0.0
    vit_k = 0.0
    folate_dfe: Optional[float] = None
    folate_total: Optional[float] = None

    for fn in raw.get("foodNutrients", []):
        nut = fn.get("nutrient") or {}
        number = str(nut.get("number") or "")
        amount = fn.get("amount")
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        name = (nut.get("name") or "").lower()

        if number in _BY_NUMBER:
            nutrients[_BY_NUMBER[number]] = amount
        elif number in _MACRO_BY_NUMBER:
            # First 208 wins (Atwater general); ignore the kJ duplicate 268.
            macros.setdefault(_MACRO_BY_NUMBER[number], amount)

        # Fatty-acid families: sum the individual n-3 / n-6 acids. The
        # undifferentiated "18:2"/"18:3" totals lack the tag and are skipped, so
        # LA/GLA/AA and ALA/EPA/DHA are each counted exactly once.
        if "n-3" in name:
            omega3 += amount
        elif "n-6" in name:
            omega6 += amount
        # Vitamin K forms (phylloquinone + menaquinones) sum to total vitamin K.
        if name.startswith("vitamin k"):
            vit_k += amount
        # Folate: prefer DFE, fall back to total.
        if number == "435":
            folate_dfe = amount
        elif number == "417":
            folate_total = amount

    if omega3 > 0:
        nutrients["omega3_g"] = omega3
    if omega6 > 0:
        nutrients["omega6_g"] = omega6
    if vit_k > 0:
        nutrients["vitamin_k_ug"] = vit_k
    folate = folate_dfe if folate_dfe is not None else folate_total
    if folate is not None:
        nutrients["folate_ug"] = folate

    return {
        "fdcId": raw.get("fdcId"),
        "description": raw.get("description", ""),
        "dataType": raw.get("dataType"),
        "nutrients": nutrients,   # per 100 g
        "macros": macros,         # per 100 g, QA only
    }
