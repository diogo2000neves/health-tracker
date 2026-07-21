#!/usr/bin/env python3
"""Phase 0 — measure the pipeline against ground truth (the eval harness).

Everything else in this rebuild is unfalsifiable without a ruler. The ruler already
exists: the user's TEMPLATES are dishes weighed on a kitchen scale, so a template-
matched meal is a (photo, measured-grams) pair — real ground truth for the perception
problem. This harness runs the pipeline stages on those photos, blind, and reports how
close each stage lands to the scale, so decisions ("does adjudication beat a single
estimate?", "does FDC grounding help the micros?") become measurements, not opinions.

What it measures:
  * MACROS (calories/protein/carbs/fat/grams) — vs the template's measured totals.
    Grounding keeps the vision macros, so the meaningful comparison here is
    SINGLE estimate  vs  ADJUDICATED. If adjudication earns its cost, its error is
    lower.
  * MICROS — there is no scale-measured micro truth, so the reference is the TEMPLATE'S
    OWN measured items grounded to FDC (known food × known grams -> database panel).
    We then ask which of the pipeline's micro panels — the MODEL estimate or the
    FDC-GROUNDED one — sits closer to that reference. This is directional (both use
    FDC), but it shows whether grounding pulls the panel toward the database truth.

Cost note: each meal costs `--samples` independent Claude estimates + one adjudication
(~3 heavy calls, ~20 min). Run it with `--limit` on a handful of template meals; it is
NOT meant to run over a whole day. This mirrors the live pipeline's ensemble, which is
independent Claude passes at different effort levels rather than a second vendor (see
audit._THIRD_ESTIMATOR) — the samples here are independent Claude runs, whose
run-to-run variance still exercises the adjudicator.

Usage:
    backend/venv/bin/python automation/nutrition-audit/eval_templates.py --limit 3
    backend/venv/bin/python automation/nutrition-audit/eval_templates.py --limit 1 --samples 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build

import adjudicate
import audit
import claude_cli
import estimate as estimate_mod
import fdc
import ground as ground_mod
import nutrients
from nutrients import meal_totals, normalize_items

log = logging.getLogger("nutrition-audit")

MACRO_FIELDS = ["calories", "protein_g", "carbs_g", "fat_g", "portion_g"]


# -- selection & truth ---------------------------------------------------------
def select_eval_meals(rows: List[Dict[str, Any]],
                      limit: Optional[int]) -> List[Dict[str, Any]]:
    """Template-matched meals with a photo and measured items — the (photo, truth)
    pairs. A template match means the stored numbers came from the scale, not a guess."""
    out = []
    for row in rows:
        if not str(row.get("template") or "").strip():
            continue
        if not str(row.get("photo_url") or "").strip():
            continue
        try:
            items = json.loads(row.get("items") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(items, list) and items:
            out.append(row)
    return out[:limit] if limit is not None else out


def _sum_nutrients(items: List[Dict[str, Any]]) -> Dict[str, float]:
    """Meal-level micronutrient totals summed across items."""
    out: Dict[str, float] = {}
    for it in items:
        for k, v in (it.get("nutrients") or {}).items():
            out[k] = out.get(k, 0.0) + v
    return out


def _rel_errors(result_items: List[Dict[str, Any]],
                truth_items: List[Dict[str, Any]]) -> Dict[str, float]:
    """Relative macro error of a result against truth, per field."""
    rt, tt = meal_totals(result_items), meal_totals(truth_items)
    out = {}
    for f in MACRO_FIELDS:
        t = tt.get(f, 0.0)
        if t > 0:
            out[f] = abs(rt.get(f, 0.0) - t) / t
    return out


def _micro_rel_error(panel: Dict[str, float],
                     reference: Dict[str, float]) -> Optional[float]:
    """Mean relative error of a micro panel against a reference panel, over the keys
    the reference actually has (so we don't penalise for keys the DB lacks)."""
    errs = []
    for k, ref in reference.items():
        if ref > 0:
            errs.append(abs(panel.get(k, 0.0) - ref) / ref)
    return round(sum(errs) / len(errs), 3) if errs else None


# -- one meal ------------------------------------------------------------------
def eval_one(drive, row: Dict[str, Any], samples: int) -> Optional[Dict[str, Any]]:
    dt = str(row.get("datetime") or "")
    note = str(row.get("note") or "")
    truth_items = normalize_items(json.loads(row.get("items") or "[]"))
    file_ids = audit.photo_file_ids(row.get("photo_url"))
    if not file_ids:
        return None
    stem = "eval_" + "".join(ch for ch in dt if ch.isdigit())[:14]
    img_paths = audit.download_photos(drive, file_ids, stem)
    if not img_paths:
        return None

    try:
        estimates = []
        for i in range(max(1, samples)):
            log.info("    estimate %d/%d ...", i + 1, samples)
            estimates.append(estimate_mod.estimate(note, img_paths))
        if len(estimates) >= 2:
            log.info("    adjudicating ...")
            adj = adjudicate.adjudicate(note, img_paths, estimates)
            adj_items = adj["items"]
        else:
            adj_items = estimates[0]["items"]
    except claude_cli.ClaudeError as exc:
        log.warning("    skip %s: %s", dt, exc)
        return None
    finally:
        for p in img_paths:
            p.unlink(missing_ok=True)

    log.info("    grounding ...")
    grounded_items, _ = ground_mod.ground([dict(x) for x in adj_items])
    # Reference micro panel: the MEASURED truth items grounded to FDC.
    truth_grounded, _ = ground_mod.ground([dict(x) for x in truth_items])

    single_items = estimates[0]["items"]
    return {
        "datetime": dt,
        "template": row.get("template"),
        "macro": {
            "single": _rel_errors(single_items, truth_items),
            "adjudicated": _rel_errors(adj_items, truth_items),
        },
        "micro": {
            "model": _micro_rel_error(_sum_nutrients(adj_items),
                                      _sum_nutrients(truth_grounded)),
            "grounded": _micro_rel_error(_sum_nutrients(grounded_items),
                                         _sum_nutrients(truth_grounded)),
        },
    }


# -- aggregation & report ------------------------------------------------------
def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def report(results: List[Dict[str, Any]]) -> None:
    if not results:
        log.info("No eval results (no template-matched meals found).")
        return
    print("\n=== MACRO error vs scale truth (mean relative error, lower is better) ===")
    print(f"{'strategy':<14}" + "".join(f"{f.replace('_g',''):>10}" for f in MACRO_FIELDS))
    for strat in ("single", "adjudicated"):
        cells = []
        for f in MACRO_FIELDS:
            m = _mean([r["macro"][strat].get(f) for r in results])
            cells.append(f"{m*100:>9.1f}%" if m is not None else f"{'-':>10}")
        print(f"{strat:<14}" + "".join(cells))

    print("\n=== MICRO error vs FDC reference (mean relative error over DB keys) ===")
    for strat in ("model", "grounded"):
        m = _mean([r["micro"][strat] for r in results])
        print(f"{strat:<14}{m*100:>9.1f}%" if m is not None else f"{strat:<14}{'-':>10}")

    n = len(results)
    macro_single = _mean([_mean(list(r["macro"]["single"].values())) for r in results])
    macro_adj = _mean([_mean(list(r["macro"]["adjudicated"].values())) for r in results])

    def pct(x: Optional[float]) -> str:
        return f"{x * 100:.1f}%" if x is not None else "n/a"
    print(f"\nOver {n} template meal(s): overall macro error "
          f"single={pct(macro_single)} -> adjudicated={pct(macro_adj)}. "
          "Adjudication wins if the second number is lower.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3,
                        help="Evaluate at most N template-matched meals (default 3).")
    parser.add_argument("--samples", type=int, default=2,
                        help="Independent estimates per meal to adjudicate (default 2).")
    parser.add_argument("--out", default=None,
                        help="Write the raw per-meal results JSON here.")
    args = parser.parse_args()

    audit.LOG_DIR.mkdir(parents=True, exist_ok=True)
    claude_cli.set_debug_dir(audit.TMP_DIR)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)])

    creds = audit.get_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    rows = audit.read_meal_rows(sheets)
    meals = select_eval_meals(rows, args.limit)
    log.info("Eval: %d template-matched meal(s) selected (samples=%d).",
             len(meals), args.samples)
    if not meals:
        log.info("No template-matched meals with photos to evaluate. Log a photo that "
                 "matches a saved template, then re-run.")
        return 0

    results = []
    for row in meals:
        log.info("Evaluating: %s (%s)", row.get("template"), row.get("datetime"))
        r = eval_one(drive, row, args.samples)
        if r:
            results.append(r)
    fdc.flush_cache()

    report(results)
    out = args.out or str(audit.LOG_DIR / f"eval_{datetime.now():%Y%m%dT%H%M%S}.json")
    Path(out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    log.info("Raw results -> %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
