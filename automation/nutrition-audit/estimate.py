#!/usr/bin/env python3
"""One INDEPENDENT meal estimate from the photo + note (Layer A + macros + micros).

This is the second opinion the pipeline produces locally with Claude. It is kept
deliberately BLIND to any other model's numbers — see the long comment below — so
that when it is later compared/adjudicated against Gemini's estimate, the two are
genuinely independent and their disagreement is real signal.

It returns a COMPLETE estimate (identity, grams, macros AND all micronutrients),
for two reasons: it doubles as the safe fallback that gets written if adjudication
or grounding later fail (degrading to exactly the old single-model behaviour), and
its micro values are the source of truth for the handful of keys FDC can't supply.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import claude_cli
import nutrients

log = logging.getLogger("nutrition-audit")

# Pinned to the model ID, NOT the "sonnet" alias: verified 2026-07-21 that on this
# machine's installed CLI the alias resolves to claude-sonnet-4-6, not the current
# claude-sonnet-5 (`--model sonnet` -> modelUsage shows claude-sonnet-4-6; `--model
# claude-sonnet-5` -> modelUsage shows claude-sonnet-5). An alias silently tracks
# whatever the CLI considers "latest", which drifted stale here — pin the ID instead.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "high"
# A high-effort call that reads image(s) and fills ~30 nutrients across every item
# is genuinely slow (6.5-9 min on a complex plate). Generous, env-overridable.
DEFAULT_TIMEOUT_S = 900

# INDEPENDENT estimate, deliberately NOT anchored on any other model's numbers.
# Why: the note ("I ate two of this", "only ate half") is an instruction RELATIVE
# TO THE PHOTO. A prior model may or may not have already applied it (handling is
# inconsistent), so if we handed Claude the prior numbers AND the note, Claude could
# apply the note a SECOND time and double-count. The photo is invariant — it always
# shows the base portion — so estimating from the image and applying the note EXACTLY
# ONCE is correct no matter what any other model did. No prior values are shown here;
# the before/after comparison is done later, in code and in the adjudicator.
PROMPT_TEMPLATE = """You are a senior clinical nutritionist and food scientist \
estimating a meal from a photo. Your numbers feed a reconciliation step and must be \
rigorous, complete, and honest about uncertainty rather than confidently round. \
Estimate INDEPENDENTLY from the image and the note below — there is no prior \
estimate to adjust; build yours from scratch.

STEP 0 — LOOK AT THE MEAL. Use the Read tool to open each image, then study it:
{img_lines}

User note about this meal (AUTHORITATIVE — overrides the image where they conflict):
{note}

HOW TO USE THE NOTE — READ CAREFULLY. The note describes the ACTUAL meal RELATIVE TO \
the photo. First estimate what is VISIBLE in the photo, then apply the note's \
adjustment EXACTLY ONCE to reach what was actually eaten. Apply it once and only \
once — never twice. Examples: photo shows one plate + note "I ate two of this" -> \
estimate the one visible plate, then multiply every portion by 2 (once). "Only ate \
half" -> estimate the visible plate, then halve it (once). "Air-fried, no oil" -> \
drop the absorbed-oil fat. A named food/brand/weight in the note overrides the \
image. If there is no note, just estimate what is visible.

Work through 1-6 IN ORDER in your reasoning before committing to numbers:

1) SCALE - calibrate portion size from real references actually in the photo \
(plate/bowl ~26-28 cm, fork ~19 cm, a 330 ml can ~12 cm, a hand). Correct for \
camera angle. If there is no reliable reference, say so and widen uncertainty.
2) INVENTORY - list every visible component AND account for what is present but \
not visible: absorbed cooking oil/butter (anything fried/sauteed/roasted), \
sauces/dressings soaked in, added sugar/syrup. Hidden fats are the single biggest \
calorie error - never skip them. List absorbed oil/butter as its OWN item with its \
own gram estimate, so it can be priced accurately later.
3) IDENTIFY - commit to the most specific identification the image supports (exact \
cut, fat level, cooking method, white vs brown, etc.). Split composite plates into \
separate items. Read any visible nutrition label and scale it to the portion - a \
label beats estimation.
4) WEIGH each item as VISIBLE in the photo (cooked, as served) in grams, including \
food partly hidden behind other food; exclude inedible parts (peel, bone, shell). \
THEN apply the note's portion adjustment once (step 0) to get the eaten amount.
5) MACROS per item - protein/carbs/fat for the final grams, then calories. \
Sanity-check: calories ~= 4*protein + 4*carbs + 9*fat (within ~10%); fix if not.
6) MICRONUTRIENTS - be thorough. For EACH item fill its `nutrients` map from that \
food's known profile, scaled to its final grams. Go through EVERY key below for \
every item and include each one the food is a genuine dietary source of, however \
small (even ~5% of a daily reference intake is worth reporting). Most whole foods \
register on 10+ keys; if an item lists only 2-3, you are almost certainly missing \
some. Omit a key ONLY when the food is genuinely not a source of it. Use EXACTLY \
these keys and units:
  grams (g): fiber_g, sugar_g, added_sugar_g, saturated_fat_g, \
monounsaturated_fat_g, polyunsaturated_fat_g, trans_fat_g, omega3_g, omega6_g
  milligrams (mg): sodium_mg, potassium_mg, calcium_mg, iron_mg, magnesium_mg, \
zinc_mg, phosphorus_mg, copper_mg, manganese_mg, chloride_mg, cholesterol_mg, \
choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg, \
vitamin_b3_mg, vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug): vitamin_a_ug, vitamin_d_ug, vitamin_k_ug, vitamin_b12_ug, \
folate_ug, biotin_ug, selenium_ug, iodine_ug

OUTPUT - return ONLY a single JSON object, no markdown fence, no prose around it:
{{"reasoning": "<your step 1-6 working>", "revision_notes": "<how you read the \
portions and, if there was a note, how you applied it exactly once>", "items": \
[{{"name": "<lowercase singular english>", "cooking_method": "<e.g. grilled>", \
"portion_g": <g>, "calories": <kcal>, "protein_g": <g>, "carbs_g": <g>, \
"fat_g": <g>, "nutrients": {{"fiber_g": <g>, "sodium_mg": <mg>}}}}], \
"confidence": <0.0-1.0>}}
Give PER-ITEM numbers only; do not sum the meal. If the photo genuinely shows no \
food, return "items": []."""


def build_prompt(note: str, img_paths: List[Path]) -> str:
    img_lines = "\n".join(f"  - {p}" for p in img_paths)
    return PROMPT_TEMPLATE.format(
        img_lines=img_lines,
        note=note.strip() if note else "(no note)",
    )


def estimate(note: str, img_paths: List[Path], *, model: str = DEFAULT_MODEL,
             effort: str = DEFAULT_EFFORT,
             timeout_s: int = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """Run one independent estimate. Returns a dict with normalized `items`
    (identity+grams+macros+nutrients), `confidence`, `reasoning`, plus the CLI's
    `_model_id`/`_cost_usd`. Raises claude_cli.ClaudeError on failure."""
    prompt = build_prompt(note, img_paths)
    result = claude_cli.call_claude_json(
        prompt, model=model, effort=effort, timeout_s=timeout_s)
    items = nutrients.normalize_items(result.get("items"))
    return {
        "items": items,
        "confidence": min(1.0, nutrients._round_num(result.get("confidence"), 2)),
        "reasoning": str(result.get("reasoning") or ""),
        "revision_notes": str(result.get("revision_notes") or ""),
        "_model_id": result.get("_model_id", model),
        "_cost_usd": result.get("_cost_usd"),
    }
