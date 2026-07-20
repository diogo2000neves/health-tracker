#!/usr/bin/env python3
"""Third independent estimator: Gemini 3.1 Pro.

Plugs into audit._THIRD_ESTIMATOR. Must return the SAME dict shape as
estimate.estimate() so adjudication/grounding work unchanged. Like the Claude
estimate, it is INDEPENDENT: it sees only the photo + note, never another model's
numbers, and applies the note exactly once.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from google import genai
from google.genai import types

import nutrients

log = logging.getLogger("nutrition-audit")

MODEL = os.environ.get("GEMINI_THIRD_MODEL", "gemini-3.1-pro-preview")
TIMEOUT_MS = int(os.environ.get("GEMINI_THIRD_TIMEOUT_MS", "180000"))

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heic"}


def _mime_for(p: Path) -> str:
    return _MIME.get(p.suffix.lower(), "image/jpeg")


PROMPT = """You are a senior clinical nutritionist and food scientist estimating a \
meal from the attached photo(s). Your numbers feed a reconciliation step and must be \
rigorous, complete, and honest about uncertainty rather than confidently round. \
Estimate INDEPENDENTLY from the image and the note below — build yours from scratch.

User note about this meal (AUTHORITATIVE — overrides the image where they conflict):
{note}

HOW TO USE THE NOTE. The note describes the ACTUAL meal RELATIVE TO the photo. First \
estimate what is VISIBLE in the photo, then apply the note's adjustment EXACTLY ONCE \
to reach what was actually eaten. Apply it once and only once — never twice ("I ate \
two of this" -> estimate the one visible plate, then multiply by 2 once; "only ate \
half" -> estimate the visible plate, then halve it once). A named food/brand/weight \
in the note overrides the image. If there is no note, just estimate what is visible.

Work through 1-6 IN ORDER before committing to numbers:
1) SCALE - calibrate portion size from real references in the photo (plate ~26-28 cm, \
fork ~19 cm, a hand). Correct for camera angle. No reference -> say so, widen uncertainty.
2) INVENTORY - list every visible component AND what is present but not visible: \
absorbed cooking oil/butter (anything fried/sauteed/roasted), soaked-in sauces, added \
sugar. Hidden fats are the biggest calorie error. List absorbed oil/butter as its OWN item.
3) IDENTIFY each item precisely (exact cut, fat level, cooking method, white vs brown). \
Split composite plates into separate items. Read any nutrition label and scale it.
4) WEIGH each item as VISIBLE (cooked, as served) in grams; then apply the note once.
5) MACROS per item - protein/carbs/fat for the final grams, then calories. Sanity-check: \
calories ~= 4*protein + 4*carbs + 9*fat (within ~10%).
6) MICRONUTRIENTS - for EACH item fill its `nutrients` map from that food's known \
profile scaled to grams. Include every key the food is a genuine source of, however \
small. Use EXACTLY these keys and units:
  grams (g): fiber_g, sugar_g, added_sugar_g, saturated_fat_g, monounsaturated_fat_g, \
polyunsaturated_fat_g, trans_fat_g, omega3_g, omega6_g
  milligrams (mg): sodium_mg, potassium_mg, calcium_mg, iron_mg, magnesium_mg, zinc_mg, \
phosphorus_mg, copper_mg, manganese_mg, chloride_mg, cholesterol_mg, choline_mg, \
vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg, vitamin_b3_mg, vitamin_b5_mg, \
vitamin_b6_mg
  micrograms (ug): vitamin_a_ug, vitamin_d_ug, vitamin_k_ug, vitamin_b12_ug, folate_ug, \
biotin_ug, selenium_ug, iodine_ug

OUTPUT - return ONLY a single JSON object, no markdown, no prose:
{{"reasoning": "<your step 1-6 working>", "revision_notes": "<how you applied the note \
once>", "items": [{{"name": "<lowercase singular english>", "cooking_method": \
"<e.g. grilled>", "portion_g": <g>, "calories": <kcal>, "protein_g": <g>, \
"carbs_g": <g>, "fat_g": <g>, "nutrients": {{"fiber_g": <g>, "sodium_mg": <mg>}}}}], \
"confidence": <0.0-1.0>}}
Give PER-ITEM numbers only; do not sum the meal. If the photo shows no food, return \
"items": []"""


def estimate(note: str, img_paths: List[Path]) -> Dict[str, Any]:
    """One independent Gemini 3.1 Pro estimate. Same return shape as
    estimate.estimate(). Raises on transport/parse failure (the caller in
    audit.gather_estimates already treats a third-estimator failure as non-fatal)."""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    parts: List[Any] = [
        types.Part.from_bytes(data=p.read_bytes(), mime_type=_mime_for(p))
        for p in img_paths
    ]
    parts.append(types.Part.from_text(
        text=PROMPT.format(note=note.strip() if note else "(no note)")))
    resp = client.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=8192,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        ),
    )
    data = json.loads(resp.text)
    items = nutrients.normalize_items(data.get("items"))
    return {
        "items": items,
        "confidence": min(1.0, nutrients._round_num(data.get("confidence"), 2)),
        "reasoning": str(data.get("reasoning") or ""),
        "revision_notes": str(data.get("revision_notes") or ""),
        "_model_id": MODEL,
        "_cost_usd": None,
    }
