#!/usr/bin/env python3
"""Phase 1 — reconcile independent estimates against the image (the adjudicator).

This replaces the old "second model overwrites the first" coup. Given N independent
estimates of the SAME photo, a fresh Claude pass looks at the image again and
reconciles them item-by-item. The rules encode where a multi-model ensemble actually
helps:

  * MEDIAN FOR MAGNITUDES — where estimates agree on grams, take the consensus;
    where they diverge, go back to the pixels and adjudicate, don't average blindly.
  * UNION FOR OMISSIONS — hidden fats/sauces/sugar are the dominant calorie error and
    are errors of OMISSION, so if ANY estimate flagged absorbed oil and it's
    physically plausible, keep it. Silence from one estimate never suppresses it.
  * NO DEFAULT WINNER — the adjudicator must justify each resolution against visible
    evidence, not pick a favourite model. To make that real the estimates are shown
    BLIND (labelled "Estimate A/B", model identity stripped and order shuffled), so
    Claude can't prefer its own or "the smart one".
  * NOTE ALREADY APPLIED — every estimate already applied the note once. The
    adjudicator is told this explicitly and must NOT re-apply any portion multiplier,
    or "I ate half" gets halved twice.

Output is the reconciled meal (identity + grams + macros + a complete micronutrient
estimate) plus a per-item disagreement record that the review log surfaces instead
of discarding.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Dict, List

import claude_cli
import nutrients

log = logging.getLogger("nutrition-audit")

# Pinned to the model ID, not the "sonnet" alias — see estimate.py's DEFAULT_MODEL
# comment: the alias resolves to a stale claude-sonnet-4-6 on this machine's CLI.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT_S = 900


def _render_estimate(label: str, items: List[Dict[str, Any]]) -> str:
    """Compact side-by-side rendering of one estimate's items (Layer A + macros).
    Micronutrients are deliberately omitted — they'd bloat the prompt and are
    recomputed on the reconciled items anyway (then grounded to a database)."""
    lines = [f"Estimate {label}:"]
    if not items:
        lines.append("  (no food)")
    for it in items:
        method = it.get("cooking_method")
        method = f", {method}" if method else ""
        lines.append(
            f"  - {it['name']}{method}: {it.get('portion_g', 0):g} g | "
            f"{it.get('calories', 0):g} kcal "
            f"P{it.get('protein_g', 0):g} C{it.get('carbs_g', 0):g} "
            f"F{it.get('fat_g', 0):g}")
    return "\n".join(lines)


PROMPT_TEMPLATE = """You are a senior clinical nutritionist adjudicating a meal \
photo. Independent estimates of the SAME meal were produced separately (each from \
the photo + note, without seeing the other). Your job is to reconcile them into ONE \
best final estimate by looking at the image yourself and deciding what the evidence \
supports — NOT by picking a favourite estimate.

STEP 0 — LOOK AT THE MEAL. Use the Read tool to open each image and study it:
{img_lines}

User note (AUTHORITATIVE for IDENTITY / what was eaten):
{note}

CRITICAL — THE PORTION ADJUSTMENT IN THE NOTE IS ALREADY DONE. Each estimate below \
already applied the note once (e.g. "I ate half" is already halved). Use the note \
only to settle IDENTITY and what is present. Do NOT re-apply any "half"/"double"/ \
fraction to the portions — that would double-count.

The independent estimates (identities, cooked grams, macros):
{estimates}

RECONCILE item by item, using the image as the tie-breaker:
1) ALIGN — match items that are the same physical component across estimates \
(different wording for the same food = one item).
2) IDENTITY — if the estimates agree on what an item is, keep it (strong signal). If \
they disagree, look at the image and choose the identification the pixels best \
support; a readable label or the note overrides.
3) GRAMS — if the estimates are within ~15% of each other, use their median. If they \
diverge more than that, re-examine the image (scale references, plate size, camera \
angle) and decide the best-supported weight; lower confidence when you had to.
4) HIDDEN COMPONENTS (union) — absorbed cooking oil/butter, soaked-in sauces/ \
dressings, added sugar/syrup are the biggest calorie errors and are usually errors of \
OMISSION. If ANY estimate flagged one AND it is physically plausible for what you see, \
KEEP it as its own item. Do not drop it just because another estimate missed it.
5) MACROS — for each reconciled item give protein/carbs/fat for the final grams, then \
calories; sanity-check calories ~= 4*protein + 4*carbs + 9*fat (within ~10%).
6) MICRONUTRIENTS — for each reconciled item fill its `nutrients` map from that food's \
known profile scaled to the final grams. Be thorough — include every key the food is a \
genuine source of, however small. Use EXACTLY these keys and units:
  grams (g): fiber_g, sugar_g, added_sugar_g, saturated_fat_g, \
monounsaturated_fat_g, polyunsaturated_fat_g, trans_fat_g, omega3_g, omega6_g
  milligrams (mg): sodium_mg, potassium_mg, calcium_mg, iron_mg, magnesium_mg, \
zinc_mg, phosphorus_mg, copper_mg, manganese_mg, chloride_mg, cholesterol_mg, \
choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg, vitamin_b3_mg, \
vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug): vitamin_a_ug, vitamin_d_ug, vitamin_k_ug, vitamin_b12_ug, \
folate_ug, biotin_ug, selenium_ug, iodine_ug

For EACH reconciled item, record how it was resolved:
  "resolution": one of "agreed" (estimates matched), "adjudicated" (they disagreed and \
you decided from the image), or "added" (a component only one estimate had, or you \
saw yourself);
  "resolution_note": one short sentence on the disagreement and why you resolved it \
this way (empty if trivially agreed).

OUTPUT — return ONLY a single JSON object, no markdown fence, no prose around it:
{{"reasoning": "<your reconciliation working>", "items": [{{"name": "<lowercase \
singular english>", "cooking_method": "<e.g. grilled>", "portion_g": <g>, \
"calories": <kcal>, "protein_g": <g>, "carbs_g": <g>, "fat_g": <g>, \
"nutrients": {{"fiber_g": <g>, "sodium_mg": <mg>}}, "resolution": "agreed", \
"resolution_note": ""}}], "confidence": <0.0-1.0>}}
Give PER-ITEM numbers only; do not sum the meal. If the image genuinely shows no \
food, return "items": []."""


def adjudicate(note: str, img_paths: List[Path], estimates: List[Dict[str, Any]],
               *, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
               timeout_s: int = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Reconcile independent `estimates` (each {items: [...], ...}) against the
    image. Estimates are shown blind and shuffled. Returns reconciled `items`
    (normalized, carrying per-item `resolution`/`resolution_note`), `confidence`,
    and `reasoning`. Raises claude_cli.ClaudeError on failure."""
    # Blind + shuffle so the adjudicator resolves on evidence, not model identity.
    order = list(range(len(estimates)))
    random.shuffle(order)
    blocks = []
    for label_i, est_i in enumerate(order):
        label = chr(ord("A") + label_i)
        blocks.append(_render_estimate(label, estimates[est_i].get("items", [])))
    prompt = PROMPT_TEMPLATE.format(
        img_lines="\n".join(f"  - {p}" for p in img_paths),
        note=note.strip() if note else "(no note)",
        estimates="\n\n".join(blocks),
    )
    result = claude_cli.call_claude_json(
        prompt, model=model, effort=effort, timeout_s=timeout_s)

    # Normalize items but preserve the resolution metadata (normalize_items drops
    # unknown keys), so re-attach it by position.
    raw_items = result.get("items") or []
    items = nutrients.normalize_items(raw_items)
    for it, raw in zip(items, raw_items):
        if isinstance(raw, dict):
            res = str(raw.get("resolution") or "").strip().lower()
            if res in ("agreed", "adjudicated", "added"):
                it["_resolution"] = res
            note_txt = str(raw.get("resolution_note") or "").strip()
            if note_txt:
                it["_resolution_note"] = note_txt[:200]

    return {
        "items": items,
        "confidence": min(1.0, nutrients._round_num(result.get("confidence"), 2)),
        "reasoning": str(result.get("reasoning") or ""),
        "_model_id": result.get("_model_id", model),
        "_cost_usd": result.get("_cost_usd"),
    }
