"""Meal estimation through a local subscription CLI (Claude or Gemini/Antigravity).

This is the *primary* estimator on the laptop deployment; the existing Gemini-API
fallback chain (see main.py's DEFAULT_MODELS) stays wired behind it. Since the
machine is on anyway and the subscription is already paid, the marginal cost of a
call here is zero — which model actually answers is one switch away, in
`llm_cli.PRIMARY_MODEL`.

**It only works on this machine, and that is inherent.** There is no `claude` or
`agy` binary on Cloud Run and neither subscription is an API key, so this whole path
is gated on `MEAL_ESTIMATOR=claude` and silently unavailable otherwise. That name
predates the Gemini option and now really means "try the local-CLI estimator
first" — kept as-is to avoid a wider rename. This is the same shape as
`QUEUE_BACKEND`: the cloud deployment keeps behaving exactly as it did, which is
what makes the parallel run possible.

## Why it reuses `automation/nutrition-audit/llm_cli.py` (claude_cli.py / agy_cli.py)

Those wrappers already solve headless CLI invocation — the ```json fence, the
echoed example object, trailing prose, the usage-limit envelope — and claude_cli's
docstring records that this took several production failures to get right. A
second copy would be a second thing to get wrong. The import is by path because
the two live in different deployment units; that is honest here precisely because
this feature is laptop-only, so there is no image boundary to cross.

## Two differences from the Gemini-API call, both forced

* **No `response_schema`.** The API path is pinned to a typed schema; a CLI has no
  equivalent, so the shape is demanded in the prompt and `extract_json_object`
  pulls the object back out. `require_key="kind"` rather than `"items"` because a
  scale screenshot legitimately returns no items.
* **Images go via disk.** Both CLIs read image paths through their own Read/file
  tool (HEIC included, verified by the audit) rather than taking bytes. The temp
  files are removed in a `finally`, and for the claude path the tool budget is
  locked to `Read` — an unrequested tool call auto-denies in headless mode and
  fails the whole call, which is exactly how the audit lost a meal on 2026-07-21
  when the model tried an unsolicited `rm -rf`. The agy path instead runs with
  cwd = the temp dir and `--dangerously-skip-permissions` (see agy_cli.py).
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

# Pinned to a model ID rather than an alias like "sonnet" for the reason
# estimate.py documents — an alias silently re-points when a new model ships,
# which would change every meal's numbers with no diff to show for it. The actual
# default now lives in llm_cli.PRIMARY_MODEL/PRIMARY_EFFORT (the one switch); these
# two are only the last-resort fallback if llm_cli can't be imported at all.
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
  "kind"                string  — "meal", "body" or "workout" (see the
                                  classification at the top)
  "body"                object  — ONLY when kind is "body"; {} otherwise
  "workout"             object  — ONLY when kind is "workout"; {} otherwise
  "meal_time"           string  — "HH:MM" 24h local, or "" when unknown
  "items"               array   — one object per ingredient; [] when kind is "body"
  "confidence"          number  — 0.1-1.0 per the rubric above; 0 when kind is "body"

Each object in "items" has exactly these keys:

  "name"            string  — REQUIRED. lowercase singular English
  "name_pt"         string  — the pt-PT display name; omit when it would be
                              identical to "name"
  "meal_time"       string  — "HH:MM" of the SITTING this item belonged to, when
                              one note logged several meals at different times (see
                              MEAL TIME above). Items sharing a time are one meal
                              and are logged as one row, so use the identical
                              string across a sitting. Omit it for an ordinary
                              one-meal log.
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

When "kind" is "workout", "workout" holds exactly these keys — and "items" is []:

  "title"          string  — the session's own name, as the app printed it
  "performed_at"   string  — "YYYY-MM-DDTHH:MM" local, read off the screen. This
                             decides which day the session lands on, so read it
                             rather than assuming today.
  "duration_min"   number  — session length in minutes, when shown
  "unit"           string  — "kg" or "lb", exactly as the app displays loads.
                             Transcribe loads in that unit; the server converts.
  "sets"           array   — ONE object per set actually performed, in order.
                             Never collapse four identical sets into one entry.
                             When several screenshots are given they are
                             consecutive scroll positions of the SAME session and
                             overlap on purpose — emit each real set once, not
                             once per image it appears in.

Each object in "sets" has:

  "exercise"      string  — REQUIRED. lowercase canonical English name
  "exercise_pt"   string  — the name as printed, in the user's language; omit
                            when it would be identical to "exercise"
  "muscle_group"  string  — primary muscle: chest, back, shoulders, biceps,
                            triceps, quads, hamstrings, glutes, calves, core,
                            forearms
  "set_type"      string  — REQUIRED. "normal", "warmup", "drop" or "failure".
                            A warm-up read as normal inflates the session by
                            20-30% and drags every strength estimate down.
  "load_type"     string  — REQUIRED. "external", "bodyweight", "assisted" or
                            "band". Get this wrong and a whole back session
                            scores as zero.
  "weight_kg"     number  — the load as printed, in the unit above. 0 is correct
                            and expected for an unloaded bodyweight set.
  "reps"          number  — REQUIRED. reps performed in THAT set
  "rir"           number  — reps in reserve, ONLY if the app shows it. Omit it
                            otherwise — never guess it. A missing RIR just isn't
                            counted as a hard set; a guessed one misreports how
                            hard the session was.

Numbers must be JSON numbers, not strings, and must not be wrapped in units.
"""


def _llm():
    """Import the shared dispatcher, adding its directory to the path.

    Returns None when it cannot be found, so a checkout without `automation/`
    degrades to the Gemini-API fallback chain instead of 500-ing every meal.
    """
    override = os.environ.get("CLAUDE_CLI_DIR", "").strip()
    if override:
        candidates = [Path(override).expanduser()]
    else:
        # backend/ingest/ -> repo root -> automation/nutrition-audit
        repo = Path(__file__).resolve().parent.parent.parent
        candidates = [repo / "automation" / "nutrition-audit"]
    for path in candidates:
        if (path / "llm_cli.py").is_file():
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                import llm_cli  # noqa: PLC0415 — located at call time by design
                return llm_cli
            except Exception:
                log.exception("llm_cli present at %s but failed to import", path)
                return None
    log.warning("llm_cli.py not found (looked in %s); Gemini-API fallback will be used",
                [str(c) for c in candidates])
    return None


def routing_status() -> str:
    """One line saying whether llm_cli routing is actually in effect.

    Exists because the way this breaks is SILENT. `CLAUDE_MEAL_MODEL` and
    `PRIMARY_MODEL` are escape hatches that pin one model and bypass the whole
    routing layer — per-task tiers, failover to agy on a spent window, `llm_mode`,
    all of it. Left over from the single-switch era they make routing dead code
    while every log line still looks healthy, which is exactly what happened on
    2026-08-21: the cheap image-router call went to Sonnet for 48 s and nobody
    could see why from the outside.
    """
    if not enabled():
        return "local-CLI estimator OFF (MEAL_ESTIMATOR unset) — Gemini API only"
    pin = os.environ.get("CLAUDE_MEAL_MODEL", "").strip()
    if pin:
        return (f"⚠️ routing BYPASSED: CLAUDE_MEAL_MODEL={pin} pins every ingest "
                f"call. Unset it to restore per-task tiers and agy failover.")
    llm = _llm()
    if llm is None:
        return "⚠️ llm_cli not importable — falling back to the Gemini API chain"
    if getattr(llm, "PRIMARY_MODEL", ""):
        return (f"⚠️ routing BYPASSED: PRIMARY_MODEL={llm.PRIMARY_MODEL} pins every "
                f"call. Unset it to restore per-task tiers and agy failover.")
    try:
        import agy_cli  # noqa: PLC0415 — located through _llm()'s path insert
        agy = "available" if agy_cli.available() else "MISSING (chain falls to Claude)"
    except Exception:
        agy = "not importable"
    return f"routing ACTIVE via llm_cli; agy {agy}"


def enabled() -> bool:
    """Whether the local-CLI estimator should be tried before the Gemini-API
    fallback chain for this deployment. Name predates the Gemini/agy option; see
    the module docstring."""
    return os.environ.get("MEAL_ESTIMATOR", "").strip().lower() == "claude"


def model() -> str:
    """The model to PIN for this call, or "" to let llm_cli route it.

    Empty is now the normal answer: routing by `source` (and its provider chain)
    lives in llm_cli, so pinning a model here would bypass the failover that exists
    for a spent usage window. `CLAUDE_MEAL_MODEL` and `PRIMARY_MODEL` remain as the
    escape hatches. What actually answered is read back off `_model_id` — see
    `answered_by`."""
    override = os.environ.get("CLAUDE_MEAL_MODEL", "").strip()
    if override:
        return override
    llm = _llm()
    return getattr(llm, "PRIMARY_MODEL", "") if llm else DEFAULT_MODEL


def answered_by(data: Dict[str, Any]) -> str:
    """Which model actually produced this answer, for the row's `model` column.

    Not the same as `model()` any more, and the difference is exactly the case
    worth recording: a spent Claude window that fell through to agy. Falls back to
    the pinned name, then to the default, so the column is never blank."""
    return str(data.get("_model_id") or "").strip() or model() or DEFAULT_MODEL


def effort() -> str:
    override = os.environ.get("CLAUDE_MEAL_EFFORT", "").strip()
    if override:
        return override
    llm = _llm()
    return llm.PRIMARY_EFFORT if llm else DEFAULT_EFFORT


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


DEFAULT_SOURCE = "ingest.claude_estimator"


def analyze(prompt: str, images: Optional[List[Tuple[bytes, str]]] = None,
            mode: Optional[str] = None, source: Optional[str] = None,
            timeout_override: Optional[int] = None) -> Dict[str, Any]:
    """Run one estimation and return the model's parsed JSON.

    Raises on any failure — an unavailable CLI, a spent usage window, a timeout, an
    unparseable answer — so the caller falls through to the Gemini-API chain.
    Deliberately does NOT retry the same provider: the task queue owns patience
    (8 attempts over ~11 minutes), and retrying a spent usage window in-process
    would just burn the request. Moving to a DIFFERENT provider is llm_cli's job,
    not a retry, and it happens inside this one call.

    `mode` is the user's routing switch, read from the sheet's `config` tab per
    request (`main._llm_mode`) rather than from the environment, so flipping it
    costs one cell on a phone instead of a service restart.

    `source` names the task, and llm_cli routes on it (see its ROUTES). It is
    supplied by `main._classify_images` for photos — a cheap first call that says
    what the image IS, so a scale screenshot can be transcribed on the fast tier
    without a meal ever being risked there.
    """
    llm = _llm()
    if llm is None:
        raise RuntimeError("llm_cli unavailable")

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
            # WebSearch so the prompt can look up a named branded product's real
            # nutrition panel (see PROMPT step 3). The allow-list is still closed:
            # Read + WebSearch only, so a model that wanders to Bash/Write is denied.
            tools = "Read WebSearch"
        else:
            # A text-only note: WebSearch only, for the same branded-product lookup
            # (see TEXT_PROMPT step 1). Naming the one allowed tool also keeps the
            # old guarantee — a model that CAN write a file may answer by writing
            # one, and with this allow-list it simply cannot.
            tools = "WebSearch"

        called_as = source or DEFAULT_SOURCE
        data = llm.call_json(
            full, model=model(), effort=effort(),
            timeout_s=timeout_override if timeout_override is not None else timeout_s(),
            require_key="kind", tools=tools, source=called_as,
            cwd=tmpdir.name if tmpdir else None, mode=mode)
        log.info("answered %s (%s, %s effort, %d image(s), mode %s)",
                 called_as, answered_by(data), effort(), len(images),
                 mode or "auto")
        return data
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()
