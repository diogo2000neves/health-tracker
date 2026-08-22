#!/usr/bin/env python3
"""Thin wrapper around the local headless `agy` (Antigravity) CLI, for Gemini calls
on the personal Google AI Pro subscription — the counterpart to claude_cli.py.

Mirrors claude_cli.call_claude_json's contract exactly (same return shape, same
`claude_cli.ClaudeError` on failure, same call log) so llm_cli.py can route between
the two without either caller knowing which CLI actually answered.

Verified flags, re-checked against a live install on **2026-08-21**:

* **Effort can travel two ways, and both work.** `agy models` lists ids that already
  carry it (`gemini-3.7-flash-high`), and a bare id plus `--effort high` is accepted
  too. A bare id with NO effort is a hard error — which is exactly how this broke on
  the first live call, with `--model gemini-3.6-flash` and nothing else. So
  `_effort_args` passes `--effort` only when the id does not already end in one,
  and either style of configuration now works.
  (The 2026-07-21 note said effort was *only* ever part of the model id. That was
  true then and is no longer the whole story — hence this paragraph.)
* `--print-timeout` defaults to 5m and will truncate a high-effort call, so it is
  pinned above the subprocess timeout.
* `--dangerously-skip-permissions` is required because headless `agy` auto-denies
  the read_file call needed to open an image and there is no interactive prompt to
  approve it. That auto-approval's blast radius is kept small by running with
  cwd = the caller's own throwaway temp dir, never the repo.
* The CLI prints housekeeping lines around the answer ("Shell cwd was reset to …").
  `extract_json_object` tolerates prose either side of the object, which is why the
  plain-text output format is fine and `--output-format json` is not needed.

**Worth knowing, not yet used:** this CLI now has `--json-schema`, which enforces
structured output the way the Gemini API's `response_schema` does. That is the one
thing the module docstring of `claude_estimator` says a CLI cannot do, and the
transcription tier is exactly where it would pay. Left alone for now because the
claude path has no equivalent and the two must answer the same contract.
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

import claude_cli  # reuses ClaudeError + the fence/prose-tolerant JSON extractor

log = logging.getLogger("nutrition-audit")

# Same unified feed claude_cli writes to: one file that sees every call regardless
# of which provider answered it, so "how many requests and to which model" stays
# answerable from one place.
_CALL_LOG = Path(__file__).resolve().parent / "logs" / "calls.jsonl"


def _log_call(*, prompt: str, model: str, effort: str, started: float,
             status: str, source: str, answered_by: Optional[str] = None,
             error: Optional[str] = None) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "model": model,
        "answered_by": answered_by,
        "effort": effort,
        "duration_s": round(time.monotonic() - started, 1),
        "status": status,
        "cost_usd": None,  # agy runs on the subscription; no per-call billing envelope
        "error": error,
        "prompt": prompt,
    }
    try:
        _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CALL_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _default_bin() -> str:
    """Absolute path when found: a systemd/launchd unit has no login shell, so a
    bare "agy" resolved only through an interactive shell's PATH would 127 exactly
    the way an unresolved `claude` did before CLAUDE_BIN was pinned."""
    local = Path.home() / ".local" / "bin" / "agy"
    return str(local) if local.exists() else "agy"


AGY_BIN = os.environ.get("AGY_BIN") or _default_bin()
TIMEOUT_S = int(os.environ.get("AGY_TIMEOUT_S", "900"))
# agy's own headless wait; keep it at/above the subprocess timeout above or a
# high-effort call on a hard prompt gets cut off before the process is killed.
PRINT_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "15m")


def available() -> bool:
    return Path(AGY_BIN).exists() or shutil.which(AGY_BIN) is not None


def call_agy_json(prompt: str, *, model: str, timeout_s: int = TIMEOUT_S,
                  source: str, require_key: str = "items", effort: str = "",
                  cwd: Optional[str] = None) -> Dict[str, Any]:
    """Run the headless `agy` CLI and return the first JSON object carrying
    `require_key`. Raises `claude_cli.ClaudeError` on any failure so callers already
    written against the Claude path (worker.py's except block, claude_estimator's
    fall-through-on-any-exception) need no changes to handle this provider too.

    `cwd`: pass the temp dir holding the prompt's referenced image(s), never the
    repo — see the module docstring on why `--dangerously-skip-permissions` needs
    that containment.
    """
    started = time.monotonic()
    try:
        result = _run_agy_json(prompt, model=model, timeout_s=timeout_s,
                               require_key=require_key, cwd=cwd, effort=effort)
    except claude_cli.ClaudeError as exc:
        _log_call(prompt=prompt, model=model, effort=effort, started=started,
                  status="error", source=source, error=str(exc))
        raise
    _log_call(prompt=prompt, model=model, effort=effort, started=started,
              status="ok", source=source, answered_by=result.get("_model_id"))
    return result


EFFORTS = ("low", "medium", "high")


def _effort_args(model: str, effort: str) -> list:
    """`--effort` only when the model id doesn't already carry one.

    Passing both is redundant; passing neither is a hard error from the CLI. This
    is the whole fix for the 2026-08-21 first-call failure, and it means either
    style of `llm_cli` configuration is valid."""
    if any(model.endswith(f"-{e}") for e in EFFORTS):
        return []
    if effort in EFFORTS:
        return ["--effort", effort]
    raise claude_cli.ClaudeError(
        f"agy model {model!r} carries no effort suffix and no effort was given "
        f"(need one of {', '.join(EFFORTS)}). Either name the model "
        f"{model}-high or pass effort=. Raised here rather than letting the CLI "
        f"return its own 'requires --effort' error, which reads like a model "
        f"problem when it is a configuration one.")


def _run_agy_json(prompt: str, *, model: str, timeout_s: int, require_key: str,
                  cwd: Optional[str], effort: str = "") -> Dict[str, Any]:
    command = [AGY_BIN, "-p", prompt, "--model", model,
               *_effort_args(model, effort),
               "--print-timeout", PRINT_TIMEOUT, "--dangerously-skip-permissions"]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL, cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise claude_cli.ClaudeError(f"agy timed out after {timeout_s}s") from exc
    except OSError as exc:
        # AGY_BIN not installed/not executable, or `cwd` doesn't exist — same
        # "can't run at all" shape as a timeout, and must degrade the same way:
        # worker.py's run_once() only catches ClaudeError, so a bare OSError here
        # would crash the coach's oneshot unit instead of releasing the job.
        raise claude_cli.ClaudeError(f"agy could not be started: {exc}") from exc
    if proc.returncode != 0:
        raise claude_cli.ClaudeError(f"agy exited {proc.returncode}: {proc.stderr[:400]}")
    # Plain text output, not an envelope like `claude --output-format json` — the
    # extractor tolerates banners/prose/```json fences around the object either way.
    try:
        obj = claude_cli.extract_json_object(proc.stdout, require_key=require_key)
    except ValueError as exc:
        raise claude_cli.ClaudeError(str(exc)) from exc
    obj["_cost_usd"] = None
    obj["_model_id"] = f"agy:{model}"
    return obj
