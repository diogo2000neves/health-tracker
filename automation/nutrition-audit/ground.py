#!/usr/bin/env python3
"""Phase 2 — ground the reconciled meal's nutrients in a food database (Layer B).

Once the adjudicator has locked WHAT each item is and HOW MANY GRAMS (the vision
problem), the micronutrient values are a lookup, not a guess. For each item we:

  1. search FDC for the food (whole-food data types first),
  2. let a light model pick the best-matching FDC entry from the candidates — or
     decline, because a WRONG match yields confidently-wrong micros, so "no match,
     keep the estimate" beats a bad match,
  3. pull that entry's measured per-100 g panel, scale it to the grams, and MERGE it
     over the model estimate: FDC wins for every key it supplies (~30 of 36 for a
     whole food); the model's value is kept only for keys FDC lacks (added sugar,
     chloride, iodine, biotin are routinely blank even in Foundation foods).

Macros stay as the vision estimate — how much protein/fat is on THIS plate is
perception, the model's strong suit — but the fat-composition split (sat/mono/poly/
omega) is taken from FDC's ratios and RESCALED to the vision total fat, so "what kind
of fat" comes from the database while "how much fat" comes from the eyes.

Every item ends with a complete, mostly database-backed nutrient panel; the grounding
report records, per item, where the numbers came from.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

import claude_cli
import fdc
import nutrients

log = logging.getLogger("nutrition-audit")

# Non-rate-limit failures a single FDC lookup can raise (HTTPError subclasses
# URLError, so a 500/404 lands here too). A failed lookup keeps the model estimate
# for that item — a complete panel must always ship.
_FDC_LOOKUP_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, KeyError,
                      json.JSONDecodeError)

# FDC matching is a text-ranking task — cheap model, low effort, short timeout.
# Pinned to the model ID, not the "sonnet" alias — see estimate.py's DEFAULT_MODEL
# comment: the alias resolves to a stale claude-sonnet-4-6 on this machine's CLI.
MATCH_MODEL = os.environ.get("FDC_MATCH_MODEL", "claude-sonnet-5")
MATCH_EFFORT = os.environ.get("FDC_MATCH_EFFORT", "low")
MATCH_TIMEOUT_S = int(os.environ.get("FDC_MATCH_TIMEOUT_S", "240"))

CANDIDATES_PER_ITEM = 6
# Guard against a bad FDC match blowing up the fat split: only rescale the fatty-acid
# breakdown to the vision fat when the two fat figures are within this ratio band.
_FAT_RESCALE_MIN, _FAT_RESCALE_MAX = 0.2, 5.0
_STOPWORDS = {"and", "with", "the", "of", "a", "raw", "fresh", "cooked"}


def _query_for(item: Dict[str, Any]) -> str:
    name = re.sub(r"[(),]", " ", str(item.get("name", "")))
    method = str(item.get("cooking_method", "")).strip()
    return re.sub(r"\s+", " ", f"{name} {method}").strip()


def _candidates_for(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    q = _query_for(item)
    if not q:
        return []
    cands = fdc.search(q, page_size=CANDIDATES_PER_ITEM)
    if not cands:  # packaged/branded item the whole-food tables don't carry
        cands = fdc.search(q, page_size=CANDIDATES_PER_ITEM, include_branded=True)
    return cands


# -- matching ------------------------------------------------------------------
_MATCH_PROMPT = """You map estimated meal items to their best USDA FoodData Central \
entry. For each item, choose the candidate that best matches its identity AND cooking \
method (a cooked food should map to a cooked entry). Prefer whole-food entries \
(Foundation / SR Legacy / Survey) over Branded unless the item is clearly a specific \
packaged product. If NO candidate is a genuinely good match, return null for that \
item — a wrong match is worse than no match. Return ONLY this JSON, no prose:
{{"matches": [{{"item_index": <int>, "fdc_id": <int or null>}}]}}

Items and candidates:
{blocks}"""


def _match_with_model(items: List[Dict[str, Any]],
                      candidates: List[List[Dict[str, Any]]]
                      ) -> Dict[int, Optional[int]]:
    """One light model call to pick an fdcId per item. Falls back to a deterministic
    token-overlap match if the call fails or returns nothing usable."""
    blocks = []
    for i, (it, cands) in enumerate(zip(items, candidates)):
        method = it.get("cooking_method", "")
        head = f'[{i}] "{it.get("name","")}"' + (f" ({method})" if method else "")
        if not cands:
            blocks.append(head + "\n     (no candidates)")
            continue
        lines = [head] + [
            f"     - fdc_id={c['fdcId']} [{c.get('dataType')}] {c.get('description','')[:70]}"
            for c in cands]
        blocks.append("\n".join(lines))
    prompt = _MATCH_PROMPT.format(blocks="\n".join(blocks))

    valid = {c["fdcId"] for cs in candidates for c in cs}
    try:
        result = claude_cli.call_claude_json(
            prompt, model=MATCH_MODEL, effort=MATCH_EFFORT,
            timeout_s=MATCH_TIMEOUT_S, require_key="matches")
        chosen: Dict[int, Optional[int]] = {}
        for m in result.get("matches", []):
            idx = m.get("item_index")
            fid = m.get("fdc_id")
            if isinstance(idx, int) and (fid is None or fid in valid):
                chosen[idx] = fid
        # Any item the model skipped falls back to deterministic matching.
        for i in range(len(items)):
            chosen.setdefault(i, _match_deterministic(items[i], candidates[i]))
        return chosen
    except claude_cli.ClaudeError as exc:
        log.warning("  ground: match call failed (%s) — deterministic fallback", exc)
        return {i: _match_deterministic(items[i], candidates[i])
                for i in range(len(items))}


def _match_deterministic(item: Dict[str, Any],
                         cands: List[Dict[str, Any]]) -> Optional[int]:
    """Best candidate by name-token overlap, preferring whole-food types. Returns
    None if nothing overlaps meaningfully (better no match than a wrong one)."""
    if not cands:
        return None
    want = {t for t in re.split(r"\W+", _query_for(item).lower())
            if t and t not in _STOPWORDS}
    if not want:
        return None
    best, best_score = None, 0.0
    for c in cands:
        have = {t for t in re.split(r"\W+", (c.get("description") or "").lower())
                if t and t not in _STOPWORDS}
        overlap = len(want & have) / len(want)
        if c.get("dataType") in ("Foundation", "SR Legacy"):
            overlap += 0.15                      # nudge toward richer whole-food data
        if overlap > best_score:
            best, best_score = c["fdcId"], overlap
    return best if best_score >= 0.5 else None


# -- merge ---------------------------------------------------------------------
def _scale_and_merge(item: Dict[str, Any], food: Dict[str, Any]
                     ) -> Tuple[Dict[str, float], int, Dict[str, Any]]:
    """Scale FDC per-100 g values to the item grams and merge over the model
    estimate. Returns (final_nutrients, n_keys_from_fdc, macro_qa)."""
    grams = nutrients._round_num(item.get("portion_g"))
    scale = grams / 100.0
    final: Dict[str, float] = dict(item.get("nutrients", {}))   # model baseline
    fdc_n = food.get("nutrients", {})

    n_from_fdc = 0
    for key, per100 in fdc_n.items():
        final[key] = per100 * scale
        n_from_fdc += 1

    # Rescale the fatty-acid split to the vision total fat: FDC gives the SHAPE
    # (ratios), the eyes give the AMOUNT. Skip if the two fats are wildly apart
    # (signals a poor match — don't propagate it into the split).
    vfat = nutrients._round_num(item.get("fat_g"))
    fdc_fat100 = food.get("macros", {}).get("fat_g")
    if vfat > 0 and fdc_fat100:
        fdc_fat_scaled = fdc_fat100 * scale
        if fdc_fat_scaled > 0:
            factor = vfat / fdc_fat_scaled
            if _FAT_RESCALE_MIN <= factor <= _FAT_RESCALE_MAX:
                for key in nutrients.FAT_COMPOSITION_KEYS:
                    if key in fdc_n:
                        final[key] = fdc_n[key] * scale * factor

    macro_qa = _macro_qa(item, food.get("macros", {}), scale)
    return nutrients.normalize_nutrients(final), n_from_fdc, macro_qa


def _macro_qa(item: Dict[str, Any], fdc_macros: Dict[str, float],
              scale: float) -> Dict[str, Any]:
    """Relative gap between the vision macros and FDC's (scaled). A large gap is a
    QA flag — usually a portion or match problem — not something we act on
    automatically, but worth logging."""
    out: Dict[str, Any] = {}
    for key in ("calories", "protein_g", "fat_g"):
        fdc_val = fdc_macros.get(key)
        vis = nutrients._round_num(item.get(key))
        if fdc_val is None or vis <= 0:
            continue
        fdc_scaled = fdc_val * scale
        if fdc_scaled > 0:
            out[key] = round((vis - fdc_scaled) / fdc_scaled, 2)
    return out


def ground(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Ground every item's nutrients in FDC. Returns (grounded_items, report).
    Never raises on FDC problems — on rate-limit or error it degrades to the model
    estimate for the affected items, because a complete estimate must always ship."""
    if not items:
        return items, {"grounded": 0, "total": 0, "keys_from_fdc": 0, "detail": []}

    try:
        candidates = [_candidates_for(it) for it in items]
    except fdc.FdcRateLimited:
        log.warning("  ground: FDC rate-limited on search — keeping model estimates")
        return items, {"grounded": 0, "total": len(items), "keys_from_fdc": 0,
                       "detail": [{"name": it.get("name"), "source": "model",
                                   "reason": "fdc_rate_limited"} for it in items]}
    except _FDC_LOOKUP_ERRORS as exc:
        # A single bad search query (e.g. a 400 from FDC on a malformed/edge-case
        # item name) must not crash the whole meal — this call must "never raise"
        # exactly like the per-item fdc.get_food() lookup below already doesn't.
        # 2026-07-21: an uncaught HTTPError here killed the launchd process mid-run.
        log.warning("  ground: FDC candidate search failed — keeping model estimates: %s", exc)
        return items, {"grounded": 0, "total": len(items), "keys_from_fdc": 0,
                       "detail": [{"name": it.get("name"), "source": "model",
                                   "reason": "fdc_error"} for it in items]}

    chosen = _match_with_model(items, candidates)

    out: List[Dict[str, Any]] = []
    detail: List[Dict[str, Any]] = []
    n_grounded = 0
    keys_from_fdc = 0
    for i, it in enumerate(items):
        grounded = dict(it)
        fid = chosen.get(i)
        entry: Dict[str, Any] = {"name": it.get("name"), "portion_g": it.get("portion_g")}
        if fid:
            try:
                food = fdc.get_food(fid)
                merged, n_keys, macro_qa = _scale_and_merge(it, food)
                grounded["nutrients"] = merged
                n_grounded += 1
                keys_from_fdc += n_keys
                entry.update({"source": "fdc", "fdc_id": fid,
                              "fdc_desc": food.get("description", "")[:70],
                              "data_type": food.get("dataType"),
                              "keys_from_fdc": n_keys, "macro_qa": macro_qa})
            except fdc.FdcRateLimited:
                log.warning("  ground: FDC rate-limited mid-run — remaining items "
                            "keep model estimates")
                entry.update({"source": "model", "reason": "fdc_rate_limited"})
                out.append(grounded)
                detail.append(entry)
                out.extend(dict(x) for x in items[i + 1:])
                detail.extend({"name": x.get("name"), "source": "model",
                               "reason": "fdc_rate_limited"} for x in items[i + 1:])
                break
            except _FDC_LOOKUP_ERRORS as exc:
                log.warning("  ground: FDC lookup failed for %s: %s", fid, exc)
                entry.update({"source": "model", "reason": "fdc_error"})
        else:
            entry.update({"source": "model", "reason": "no_match"})
        out.append(grounded)
        detail.append(entry)

    fdc.flush_cache()
    report = {"grounded": n_grounded, "total": len(items),
              "keys_from_fdc": keys_from_fdc, "detail": detail}
    return out, report
