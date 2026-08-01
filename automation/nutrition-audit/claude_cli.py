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

Tool access is locked to Read (--allowedTools): every prompt here only ever needs it
to open the image(s). Headless `-p` can't prompt for permission, so an unrequested
tool call auto-denies and fails the whole call — which is exactly what happened in
production on 2026-07-21 (right after pinning to claude-sonnet-5): the model
spontaneously tried `Bash("rm -rf /tmp/nutriimg")` as unsolicited "cleanup" (the
caller already deletes the temp images itself in a `finally`), got denied, and the
meal was skipped. Locking the tool list turns that failure mode into "can't happen"
instead of "hope the model doesn't wander."
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("nutrition-audit")

# Every caller of this module funnels through call_claude_json, so this is the one
# place that sees every prompt sent to Claude — the coach's reports and chat, and
# the nutrition-audit pipeline's estimate/adjudicate/ground/eval stages alike. One
# line per call: when, which model was asked for and which one actually answered,
# how long it took, what it cost, and the prompt itself. Meant for answering "how
# many requests are we making and to which model" while the coach is still new
# enough to need watching closely.
_CALL_LOG = Path(__file__).resolve().parent / "logs" / "calls.jsonl"


def _log_call(*, prompt: str, model: str, effort: str, started: float,
             status: str, source: str, answered_by: Optional[str] = None,
             cost_usd: Optional[float] = None, error: Optional[str] = None) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "model": model,
        "answered_by": answered_by,
        "effort": effort,
        "duration_s": round(time.monotonic() - started, 1),
        "status": status,
        "cost_usd": cost_usd,
        "error": error,
        "prompt": prompt,
    }
    try:
        _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CALL_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# Falls back to whatever `claude` is on PATH. The old default was a hard-coded
# MacBook nvm path; on the laptop that resolves to nothing and every call fails
# identically ("claude exited 127"). systemd units set CLAUDE_BIN explicitly
# because a unit has no login shell and fnm's shim is per-shell — see
# deploy/env.example.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

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
                     timeout_s: int, source: str, require_key: str = "items",
                     tools: str = "Read") -> Dict[str, Any]:
    """Run the headless CLI and return the first JSON object carrying `require_key`.
    Attaches `_cost_usd` and `_model_id` from the CLI envelope. Raises ClaudeError
    on any failure so the caller decides how to degrade.

    `source` identifies the caller (e.g. "audit.estimate", "coach") so the usage
    log can be broken down by feature, not just totalled.

    Every call — answered or failed — is recorded to `_CALL_LOG` before this
    returns or raises, regardless of which caller made it.
    """
    started = time.monotonic()
    try:
        result = _run_claude_json(prompt, model=model, effort=effort,
                                  timeout_s=timeout_s, require_key=require_key,
                                  tools=tools)
    except ClaudeError as exc:
        _log_call(prompt=prompt, model=model, effort=effort, started=started,
                  status="error", source=source, error=str(exc))
        raise
    _log_call(prompt=prompt, model=model, effort=effort, started=started,
              status="ok", source=source, answered_by=result.get("_model_id"),
              cost_usd=result.get("_cost_usd"))
    return result


def _run_claude_json(prompt: str, *, model: str, effort: str,
                     timeout_s: int, require_key: str,
                     tools: str) -> Dict[str, Any]:
    """The actual CLI invocation. See `call_claude_json` for the public contract.

    `tools` is the exact tool budget for the call. "Read" is what the audit needs (to
    open the meal images). Pass "" for a pure reasoning call: a prompt that needs no
    tools should be given none, because a model that *can* write a file may decide to
    answer by writing one — which is exactly what happened the first time the coach's
    prompt was run through here. It did the work, wrote the JSON to disk, and returned
    a prose summary, so the caller found no JSON in the answer at all.
    """
    if tools:
        # The audit's invocation, unchanged: it is production and it works.
        command = [CLAUDE_BIN, "-p", prompt, "--model", model, "--effort", effort,
                   "--output-format", "json", "--allowedTools", tools]
    else:
        # Deny the tools that let a model answer by side effect instead of by
        # replying. `--tools ""` is not usable here: the flag is variadic, so an
        # empty value silently swallowed the prompt and the CLI then answered
        # whatever it found on stdin. Hence an explicit deny list, and `-p` LAST so
        # it terminates that list.
        command = [CLAUDE_BIN, "--model", model, "--effort", effort,
                   "--output-format", "json",
                   "--disallowedTools", "Write", "Edit", "NotebookEdit", "Bash",
                   "Task", "WebFetch", "WebSearch",
                   "-p", prompt]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
            # Never inherit stdin. A headless call that reads the caller's stdin will
            # happily answer something that was never asked.
            stdin=subprocess.DEVNULL,
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
