#!/usr/bin/env python3
"""Push new Claude API calls from calls.jsonl into the shared Sheet (runs locally).

claude_cli.call_claude_json already logs one JSON line per call — every stage of
the audit, the coach worker, and the backend's claude_estimator alike (see
claude_cli.py's module docstring) — to logs/calls.jsonl. That file answers "what
happened" but only on this machine; this script mirrors it into a `claude_calls`
tab on the same Sheet the rest of the pipeline shares, so usage/cost can be
tracked without shelling in.

Incremental and idempotent: how many lines have already been pushed is tracked in
logs/calls.synced (a line count). Each run only appends the lines beyond that,
in one batched request. Run standalone; usage:
    backend/venv/bin/python automation/nutrition-audit/sync_calls_to_sheet.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from googleapiclient.discovery import build

import audit

log = logging.getLogger("nutrition-audit")

CALLS_LOG = Path(__file__).resolve().parent / "logs" / "calls.jsonl"
SYNCED_STATE = Path(__file__).resolve().parent / "logs" / "calls.synced"

# A tab this script owns. Unlike audit.py's meal_reviews tab, this data can't be
# regenerated from a re-run (calls.jsonl only grows forward, and old entries are
# never re-derivable), so — deliberately, unlike ensure_reviews_tab — a header
# mismatch here is left alone rather than cleared and rewritten.
CALLS_TAB = "claude_calls"
CALLS_HEADERS = [
    "at", "source", "model", "answered_by", "effort", "duration_s",
    "status", "cost_usd", "error", "prompt_preview", "prompt_chars",
]
PROMPT_PREVIEW_CHARS = 400


def ensure_calls_tab(sheets) -> None:
    """Create the claude_calls tab with its header if the tab is entirely absent.
    Idempotent — safe to call before every append."""
    meta = sheets.spreadsheets().get(spreadsheetId=audit.SHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if CALLS_TAB not in titles:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=audit.SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": CALLS_TAB}}}]},
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=audit.SHEET_ID, range=f"{CALLS_TAB}!A1",
            valueInputOption="RAW", body={"values": [CALLS_HEADERS]},
        ).execute()


def _read_new_lines() -> Tuple[List[str], int, int]:
    """Returns (new raw lines, already-synced count, total line count)."""
    if not CALLS_LOG.exists():
        return [], 0, 0
    lines = CALLS_LOG.read_text().splitlines()
    total = len(lines)
    synced = 0
    if SYNCED_STATE.exists():
        try:
            synced = int(SYNCED_STATE.read_text().strip())
        except ValueError:
            synced = 0
    # Clamp against a stale/corrupt state file pointing past the current log.
    synced = max(0, min(synced, total))
    return lines[synced:], synced, total


def _build_row(entry: Dict[str, Any]) -> List[Any]:
    prompt = entry.get("prompt") or ""
    preview = prompt[:PROMPT_PREVIEW_CHARS]
    if len(prompt) > PROMPT_PREVIEW_CHARS:
        preview += "…"
    return [
        entry.get("at"), entry.get("source"), entry.get("model"),
        entry.get("answered_by"), entry.get("effort"), entry.get("duration_s"),
        entry.get("status"), entry.get("cost_usd"), entry.get("error"),
        preview, len(prompt),
    ]


def _write_synced_state(total: int) -> None:
    tmp = SYNCED_STATE.with_suffix(".tmp")
    tmp.write_text(str(total))
    os.replace(tmp, SYNCED_STATE)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)])

    new_lines, synced, total = _read_new_lines()
    if not new_lines:
        log.info("claude_calls: up to date (%d call(s) already synced).", synced)
        return 0

    rows = []
    for raw in new_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("claude_calls: skipping malformed log line: %r", raw[:200])
            continue
        rows.append(_build_row(entry))

    if not rows:
        _write_synced_state(total)
        log.info("claude_calls: no parseable new calls (%d -> %d).", synced, total)
        return 0

    # Same fallback the audit job uses: the legacy combined token if this machine
    # already has one, otherwise the service account (Sheets-only here — no Drive
    # access is needed to log calls).
    legacy = audit.get_credentials()
    creds = legacy if legacy is not None else audit.sheets_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    ensure_calls_tab(sheets)
    sheets.spreadsheets().values().append(
        spreadsheetId=audit.SHEET_ID, range=f"{CALLS_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    _write_synced_state(total)
    log.info("claude_calls: synced %d new call(s) (%d -> %d).",
             len(rows), synced, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
