#!/usr/bin/env python3
"""Thin wrapper around the local headless `claude` CLI that returns parsed JSON.

Every reasoning stage in the pipeline (independent estimate, adjudication, FDC
matching) is one `claude -p` call that must return a single JSON object. This
module owns the transport and the robust parsing so the three callers don't each
re-implement the "strip the ```json fence, ignore the echoed example object,
survive trailing prose" logic that took a few production failures to get right.

The image(s) are NOT passed as CLI flags — the prompt names the on-disk paths and
the CLI's own Read tool opens them (HEIC included, verified). So a call is just a
prompt string plus model/effort/timeout knobs.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("nutrition-audit")

CLAUDE_BIN = os.environ.get(
    "CLAUDE_BIN", "/Users/dneves/.nvm/versions/node/v22.12.0/bin/claude")

# Where a parse failure dumps the raw model answer for inspection. Set by the
# orchestrator (audit.py) at startup; defaults next to this file so imports work.
_DEBUG_DIR = Path(__file__).resolve().parent / "logs" / "tmp"


def set_debug_dir(path: Path) -> None:
    global _DEBUG_DIR
    _DEBUG_DIR = path


class ClaudeError(RuntimeError):
    """Any transport/permission/parse failure. Callers catch this and skip the
    meal (or fall back), so a bad call can never corrupt data."""


def call_claude_json(prompt: str, *, model: str, effort: str,
                     timeout_s: int, require_key: str = "items") -> Dict[str, Any]:
    """Run the headless CLI and return the first JSON object carrying `require_key`.
    Attaches `_cost_usd` and `_model_id` from the CLI envelope. Raises ClaudeError
    on any failure so the caller decides how to degrade."""
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", model,
             "--effort", effort, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"claude timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise ClaudeError(f"claude exited {proc.returncode}: {proc.stderr[:400]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeError(f"claude envelope not JSON: {proc.stdout[:200]!r}") from exc
    if envelope.get("is_error"):
        raise ClaudeError(f"claude reported error: {envelope.get('result')!r}")
    if envelope.get("permission_denials"):
        raise ClaudeError(f"claude permission denied: {envelope['permission_denials']}")
    result_text = envelope.get("result") or ""
    try:
        obj = extract_json_object(result_text, require_key=require_key)
    except ValueError as exc:
        _save_debug(result_text)
        raise ClaudeError(str(exc)) from exc
    obj["_cost_usd"] = envelope.get("total_cost_usd")
    obj["_model_id"] = _first_model_id(envelope, model)
    return obj


def extract_json_object(text: str, *, require_key: str = "items") -> Dict[str, Any]:
    """Return the first complete JSON object in the answer that carries
    `require_key`. Robust to: a ```json fence, surrounding prose, and a smaller
    JSON-ish snippet (the example object echoed from the prompt) appearing BEFORE
    the real answer. raw_decode reads one complete object and ignores trailing
    data, so `{obj} ...more...` parses fine."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    decoder = json.JSONDecoder()
    i = 0
    while True:
        brace = text.find("{", i)
        if brace == -1:
            raise ValueError(f"no {require_key!r}-bearing JSON object in claude "
                             f"output: {text[:200]!r}")
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            i = brace + 1                 # not a valid object here — try the next {
            continue
        if isinstance(obj, dict) and require_key in obj:
            return obj
        i = end                           # a valid but wrong object — skip past it


def _save_debug(text: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / "last_parse_failure.txt").write_text(text)
    except OSError:
        pass


def _first_model_id(envelope: Dict[str, Any], fallback: str) -> str:
    usage = envelope.get("modelUsage") or {}
    for name in usage:                      # the real answering model, not the label
        if "haiku" not in name:             # haiku is CC's own bookkeeping call
            return name
    return fallback
