#!/usr/bin/env python3
"""Third estimator via the local `gemini` CLI — subscription-backed, NO API key.

Mirrors the `claude` CLI path in claude_cli.py: we shell out to a logged-in CLI whose
auth and usage come from the personal subscription, exactly like `claude -p`. Plugs
into audit._THIRD_ESTIMATOR and returns the SAME dict shape as estimate.estimate().

It reuses the identical independent-estimate PROMPT from estimate.py (same steps, same
36 nutrient keys), so Gemini and Claude answer the same question — only the images are
handed over the CLI's way instead of Claude's Read tool.

TWO THINGS TO CONFIRM for your CLI (both env-overridable):
  * GEMINI_BIN (default "gemini") and GEMINI_CLI_MODEL (default "gemini-3.1-pro-preview").
  * How it takes images: this references each photo with `@<path>` inside the prompt,
    which Google's gemini-cli reads inline. If your CLI takes images another way, adjust
    the argv / _build_prompt below.
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

GEMINI_BIN = os.environ.get("GEMINI_BIN", "gemini")
MODEL = os.environ.get("GEMINI_CLI_MODEL", "gemini-3.1-pro-preview")
TIMEOUT_S = int(os.environ.get("GEMINI_CLI_TIMEOUT_S", "900"))


def available() -> bool:
    """True if the gemini CLI is on PATH — the pipeline wires the third estimator in
    only when it is, so a missing CLI just means Gemini(row) + Claude, no error."""
    return shutil.which(GEMINI_BIN) is not None


def _build_prompt(note: str, img_paths: List[Path]) -> str:
    # `@<path>` makes gemini-cli read the file inline. Same prompt body as the Claude
    # estimate, so the two opinions are genuinely comparable.
    img_refs = "\n".join(f"  @{p}" for p in img_paths)
    return estimate_mod.PROMPT_TEMPLATE.format(
        img_lines=img_refs, note=note.strip() if note else "(no note)")


def estimate(note: str, img_paths: List[Path]) -> Dict[str, Any]:
    """One independent Gemini estimate via the CLI. Same return shape as
    estimate.estimate(). Raises claude_cli.ClaudeError on any failure (the caller in
    audit.gather_estimates treats a third-estimator failure as non-fatal)."""
    prompt = _build_prompt(note, img_paths)
    try:
        # --approval-mode yolo: this is an unattended background job, so never block on
        # a tool-approval prompt (it would hang until the timeout). The only input is the
        # user's own meal photo.
        proc = subprocess.run(
            [GEMINI_BIN, "-m", MODEL, "-p", prompt, "--approval-mode", "yolo"],
            capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise claude_cli.ClaudeError(f"gemini cli timed out after {TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise claude_cli.ClaudeError(
            f"gemini cli exited {proc.returncode}: {proc.stderr[:400]}")
    data = claude_cli.extract_json_object(proc.stdout)  # tolerates prose/fences
    items = nutrients.normalize_items(data.get("items"))
    return {
        "items": items,
        "confidence": min(1.0, nutrients._round_num(data.get("confidence"), 2)),
        "reasoning": str(data.get("reasoning") or ""),
        "revision_notes": str(data.get("revision_notes") or ""),
        "_model_id": MODEL,
        "_cost_usd": None,
    }
