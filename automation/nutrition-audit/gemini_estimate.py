#!/usr/bin/env python3
"""Third estimator via the local `agy` CLI (Antigravity) — subscription, NO API key.

Mirrors the `claude` CLI path in claude_cli.py: we shell out to a logged-in agent CLI
whose auth/usage come from the personal Google AI Pro subscription. Plugs into
audit._THIRD_ESTIMATOR and returns the SAME dict shape as estimate.estimate().

Why `agy` and not `gemini`: Google retired individual sign-in on the legacy
`@google/gemini-cli` (it now fails with IneligibleTierError -> "migrate to Antigravity").
The Antigravity CLI (`agy`) is the supported terminal client for individual
subscriptions and runs headless with `-p`.

MODEL CHOICE — deliberately gemini-3.6-flash at HIGH effort, not 3.1-pro:
  * backend/ingest/main.py documents that gemini-3.1-pro-preview is OLDER than
    the Flash line and LOSES to it on multimodal understanding (MMMU-Pro) — and this
    workload is photos of food, i.e. exactly multimodal.
  * The original diagnosis was that Gemini's errors here are a SPEED tax, not
    incompetence: cloud ingest runs flash rushed under a ~105 s deadline. Running the
    same-family model at HIGH effort with no deadline makes this a genuinely DELIBERATE
    second Gemini opinion, decorrelated from the rushed ingest one — which is what makes
    it worth adjudicating against.
  * gemini-3.6-flash (released 2026-07-21) is the successor to gemini-3.5-flash and is
    now cloud ingest's primary model (see DEFAULT_MODELS in main.py), so the "same
    family as ingest's primary" rationale means this should track it, not stay pinned
    to the previous generation.
Override with AGY_MODEL (e.g. "gemini-3.1-pro-preview") if you want a different one.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import claude_cli          # reuse the robust JSON extractor + ClaudeError
import estimate as estimate_mod   # reuse the exact independent-estimate prompt
import nutrients

log = logging.getLogger("nutrition-audit")


def _default_bin() -> str:
    """Absolute path when we can find it: launchd runs with a minimal PATH that does
    NOT include ~/.local/bin, so relying on the bare name would break the scheduled job
    (the same reason CLAUDE_BIN is absolute)."""
    local = Path.home() / ".local" / "bin" / "agy"
    return str(local) if local.exists() else "agy"


AGY_BIN = os.environ.get("AGY_BIN", _default_bin())
# Verified against `agy models` (2026-07-21): effort is part of the id
# (…-high/-medium/-low), not a separate flag. gemini-3.1-pro-high is also available if
# you ever want to switch.
MODEL = os.environ.get("AGY_MODEL", "gemini-3.6-flash-high")
TIMEOUT_S = int(os.environ.get("AGY_TIMEOUT_S", "900"))
# agy's own headless wait; its default is 5m, which would cut off a high-effort call on
# a complex plate. Keep it at/above our subprocess timeout.
PRINT_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "15m")


def available() -> bool:
    """True if the agy CLI is usable — the pipeline wires the third estimator in only
    when it is, so a missing CLI just means Gemini(row) + Claude, no error."""
    return Path(AGY_BIN).exists() or shutil.which(AGY_BIN) is not None


def estimate(note: str, img_paths: List[Path]) -> Dict[str, Any]:
    """One independent Gemini estimate via the agy CLI. Same return shape as
    estimate.estimate(). Raises claude_cli.ClaudeError on any failure (the caller in
    audit.gather_estimates treats a third-estimator failure as non-fatal).

    Uses the IDENTICAL prompt as the Claude estimate — including its "open each image
    with the Read tool" step, which works because agy is an agent harness with file
    tools, just like the claude CLI. So the two opinions answer the same question."""
    prompt = estimate_mod.build_prompt(note, img_paths)
    # --dangerously-skip-permissions: headless agy AUTO-DENIES tool permissions (it
    # can't prompt), so without this the read_file call is refused and no estimate is
    # produced. The blast radius is kept small by running with cwd = the throwaway photo
    # temp dir rather than the repo. If you'd rather not auto-approve, the alternative is
    # a scoped `permissions.allow` rule (e.g. read_file(<tmp dir>)) in agy's settings.json.
    try:
        proc = subprocess.run(
            [AGY_BIN, "-p", prompt, "--model", MODEL,
             "--print-timeout", PRINT_TIMEOUT, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=TIMEOUT_S,
            cwd=str(img_paths[0].parent) if img_paths else None)
    except subprocess.TimeoutExpired as exc:
        raise claude_cli.ClaudeError(f"agy timed out after {TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise claude_cli.ClaudeError(
            f"agy exited {proc.returncode}: {proc.stderr[:400]}")
    # Plain text output: the extractor finds the first JSON object carrying `items`,
    # tolerating banners, prose and ```json fences around it.
    data = claude_cli.extract_json_object(proc.stdout)
    items = nutrients.normalize_items(data.get("items"))
    return {
        "items": items,
        "confidence": min(1.0, nutrients._round_num(data.get("confidence"), 2)),
        "reasoning": str(data.get("reasoning") or ""),
        "revision_notes": str(data.get("revision_notes") or ""),
        "_model_id": f"agy:{MODEL}",
        "_cost_usd": None,
    }
