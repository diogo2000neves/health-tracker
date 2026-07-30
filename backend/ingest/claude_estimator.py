"""Meal estimation through the local `claude` CLI, on the personal subscription.

This is the *primary* estimator on the laptop deployment; Gemini stays wired behind
it as the fallback. Claude is the better estimator, and since the machine is on
anyway and the subscription is already paid, the marginal cost of using it is zero.

**It only works on this machine, and that is inherent.** There is no `claude`
binary on Cloud Run and the subscription is not an API key, so this whole path is
gated on `MEAL_ESTIMATOR=claude` and silently unavailable otherwise. That is the
same shape as `QUEUE_BACKEND`: the cloud deployment keeps behaving exactly as it
did, which is what makes the parallel run possible.

## Why it reuses `automation/nutrition-audit/claude_cli.py`

That wrapper already solves headless `claude` invocation — the ```json fence, the
echoed example object, trailing prose, the usage-limit envelope — and its docstring
records that this took several production failures to get right. A second copy
would be a second thing to get wrong. The import is by path because the two live in
different deployment units; that is honest here precisely because this feature is
laptop-only, so there is no image boundary to cross.

## Two differences from the Gemini call, both forced

* **No `response_schema`.** Gemini is pinned to a typed schema; the CLI has no
  equivalent, so the shape is demanded in the prompt and `extract_json_object`
  pulls the object back out. `require_key="kind"` rather than `"items"` because a
  scale screenshot legitimately returns no items.
* **Images go via disk.** The CLI reads image paths with its own Read tool (HEIC
  included, verified by the audit) rather than taking bytes. The temp files are
  removed in a `finally`, and the tool budget is locked to `Read` — an unrequested
  tool call auto-denies in headless mode and fails the whole call, which is exactly
  how the audit lost a meal on 2026-07-21 when the model tried an unsolicited
  `rm -rf`.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("claude-estimator")

# Sonnet 5 at high effort: the user's chosen default for ingest. Pinned to the
# model ID rather than the "sonnet" alias for the reason estimate.py documents —
# an alias silently re-points when a new model ships, which would change every
# meal's numbers with no diff to show for it.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "high"
# 900 s, matching the audit's `estimate.DEFAULT_TIMEOUT_S` and for the same measured
# reason: a high-effort call that reads image(s) and fills ~30 nutrients per item
# takes 6.5-9 minutes on a complex plate. Nothing waits on /process
# (ack-then-analyse), so the only cost of a generous timeout is a slow row.
#
# Too SHORT is the real failure mode here — it would discard a good Claude answer
# and fall back to Gemini on exactly the complicated plates where Claude is most
# worth having. The rest of the local budget is sized around this number:
# gunicorn --timeout 1200 and LOCAL_QUEUE_DISPATCH_TIMEOUT_S 1200 both exceed
# 900 (claude) + 105 (gemini deadline) + 60 (one gemini call) + ~5 (sheet writes).
DEFAULT_TIMEOUT_S = 900

# Appended to the existing prompt. The Gemini path gets this structure from
# `RESPONSE_SCHEMA`; here it has to be asked for. `reasoning` is demanded FIRST for
# the same reason `RESPONSE_SCHEMA.property_ordering` puts it first — making the
# model work through scale and hidden fats before committing to numbers is
# documented as the main accuracy lever, and it is lost if the JSON leads with the
# totals.
JSON_INSTRUCTIONS = """

================================ OUTPUT FORMAT =================================

Reply with ONE JSON object and nothing else — no prose before or after it, no
markdown fence. Emit the keys in exactly this order:

  "reasoning"           string  — your working: scale calibration, hidden fats,
                                  per-item sanity check. WRITE THIS FIRST, before
                                  any number below. It is not decoration; committing
                                  to totals before reasoning is what makes estimates
                                  drift.
  "kind"                string  — "meal" or "body" (see the classification at the top)
  "body"                object  — ONLY when kind is "body"; {} otherwise
  "meal_time"           string  — "HH:MM" 24h local, or "" when unknown
  "template"            string  — a known template's name verbatim, or ""
  "template_scale"      number  — fraction of the template eaten (1 = all), or null
  "save_template_name"  string  — name to save this meal under, or ""
  "items"               array   — one object per ingredient; [] when kind is "body"
  "confidence"          number  — 0.1-1.0 per the rubric above; 0 when kind is "body"

Each object in "items" has exactly these keys:

  "name"            string  — REQUIRED. lowercase singular English
  "name_pt"         string  — the pt-PT display name; omit when it would be
                              identical to "name"
  "cooking_method"  string  — e.g. "fried", "grilled", "raw", "air-fried"
  "portion_g"       number  — REQUIRED. edible weight of THIS ingredient in grams.
                              Never omit it and never send 0: the grams are the
                              magnitude every later step reconciles against, and a
                              zero silently erases the item from the daily totals.
  "calories"        number  — REQUIRED
  "protein_g"       number  — REQUIRED
  "carbs_g"         number  — REQUIRED
  "fat_g"           number  — REQUIRED
  "nutrients"       object  — the per-nutrient map described above; include every
                              key you can estimate and omit the negligible ones.
                              Leaving this out blanks the micronutrient columns for
                              the whole day.

Numbers must be JSON numbers, not strings, and must not be wrapped in units.
"""


def _cli():
    """Import the audit's CLI wrapper, adding its directory to the path.

    Returns None when it cannot be found, so a checkout without `automation/`
    degrades to Gemini instead of 500-ing every meal.
    """
    override = os.environ.get("CLAUDE_CLI_DIR", "").strip()
    if override:
        candidates = [Path(override).expanduser()]
    else:
        # backend/ingest/ -> repo root -> automation/nutrition-audit
        repo = Path(__file__).resolve().parent.parent.parent
        candidates = [repo / "automation" / "nutrition-audit"]
    for path in candidates:
        if (path / "claude_cli.py").is_file():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                import claude_cli  # noqa: PLC0415 — located at call time by design
                return claude_cli
            except Exception:
                log.exception("claude_cli present at %s but failed to import", path)
                return None
    log.warning("claude_cli.py not found (looked in %s); Gemini will be used",
                [str(c) for c in candidates])
    return None


def enabled() -> bool:
    """Whether Claude should be tried before Gemini for this deployment."""
    return os.environ.get("MEAL_ESTIMATOR", "").strip().lower() == "claude"


def model() -> str:
    return os.environ.get("CLAUDE_MEAL_MODEL", "").strip() or DEFAULT_MODEL


def effort() -> str:
    return os.environ.get("CLAUDE_MEAL_EFFORT", "").strip() or DEFAULT_EFFORT


def timeout_s() -> int:
    try:
        return int(os.environ.get("CLAUDE_MEAL_TIMEOUT_S", "").strip()
                   or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _write_images(images: List[Tuple[bytes, str]], into: str) -> List[Path]:
    """Spill the photos to disk for the CLI's Read tool. Extensions matter — the
    tool dispatches on them."""
    paths: List[Path] = []
    for index, (data, mime) in enumerate(images):
        ext = "png" if "png" in (mime or "") else "jpg"
        path = Path(into) / f"meal_{index}.{ext}"
        path.write_bytes(data)
        paths.append(path)
    return paths


def analyze(prompt: str, images: Optional[List[Tuple[bytes, str]]] = None,
            ) -> Dict[str, Any]:
    """Run one estimation and return the model's parsed JSON.

    Raises on any failure — an unavailable CLI, a spent usage window, a timeout, an
    unparseable answer — so the caller falls through to Gemini. Deliberately does
    NOT retry: the task queue owns patience (8 attempts over ~11 minutes), and
    retrying a spent 5-hour window in-process would just burn the request.
    """
    cli = _cli()
    if cli is None:
        raise RuntimeError("claude_cli unavailable")

    images = images or []
    full = prompt + JSON_INSTRUCTIONS
    tmpdir: Optional[tempfile.TemporaryDirectory] = None
    try:
        if images:
            tmpdir = tempfile.TemporaryDirectory(prefix="ht-meal-")
            paths = _write_images(images, tmpdir.name)
            listing = "\n".join(f"  {p}" for p in paths)
            full = (f"Read the image file(s) at these paths and analyse them:\n"
                    f"{listing}\n\n" + full)
            tools = "Read"
        else:
            # A text-only note needs no tools at all, and a model that CAN write a
            # file may answer by writing one — which is exactly what happened the
            # first time the coach's prompt went through this wrapper.
            tools = ""

        data = cli.call_claude_json(
            full, model=model(), effort=effort(), timeout_s=timeout_s(),
            require_key="kind", tools=tools)
        log.info("claude estimated a meal (%s, %s effort, %d image(s))",
                 data.get("_model_id") or model(), effort(), len(images))
        return data
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()
