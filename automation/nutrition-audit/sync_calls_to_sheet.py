#!/usr/bin/env python3
"""Push new Claude API calls from calls.jsonl into the shared Sheet (runs locally).

claude_cli.call_claude_json already logs one JSON line per call — every stage of
the audit, the coach worker, and the backend's claude_estimator alike (see
claude_cli.py's module docstring) — to logs/calls.jsonl. That file answers "what
happened" but only on this machine; this script mirrors it into a `claude_calls`
tab on the same Sheet the rest of the pipeline shares, so usage/cost can be
tracked without shelling in.

The full prompt (sometimes 50k+ characters — past what a Sheets cell can even
hold) is uploaded to a Drive folder as one .txt file per call, and the row only
carries a short preview plus a link to it — see _upload_prompt.

Incremental and idempotent: how many lines have already been pushed is tracked in
logs/calls.synced (a line count). Each run only appends the lines beyond that,
in one batched Sheets request (Drive uploads are necessarily one call each).
Run standalone; usage:
    backend/venv/bin/python automation/nutrition-audit/sync_calls_to_sheet.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import audit

log = logging.getLogger("nutrition-audit")

CALLS_LOG = Path(__file__).resolve().parent / "logs" / "calls.jsonl"
SYNCED_STATE = Path(__file__).resolve().parent / "logs" / "calls.synced"

# A tab this script owns. Unlike audit.py's meal_reviews tab, this data can't be
# regenerated from a re-run (calls.jsonl only grows forward, and old entries are
# never re-derivable), so — deliberately, unlike ensure_reviews_tab — an
# unrecognised header is left alone rather than cleared and rewritten. A header
# that's merely an OLDER, shorter version of this one is still upgraded in place
# (see ensure_calls_tab) — that's schema evolution, not a foreign layout.
CALLS_TAB = "claude_calls"
CALLS_HEADERS = [
    "at", "source", "model", "answered_by", "effort", "duration_s",
    "status", "cost_usd", "error", "prompt_preview", "prompt_chars",
    "prompt_drive_link",
]
PROMPT_PREVIEW_CHARS = 400

# Drive-side home for the full prompt text. drive.file scope only ever sees
# files this credential itself created, so a name lookup is enough to find it
# again next run — no id needs to be persisted anywhere.
PROMPTS_FOLDER_NAME = "claude_prompts"


def ensure_calls_tab(sheets) -> None:
    """Create the claude_calls tab with its header if the tab is entirely absent,
    or upgrade the header in place if it's an older prefix of CALLS_HEADERS (a
    purely additive schema change — existing rows just gain a blank trailing
    cell, which is correct: they predate the new column). Idempotent — safe to
    call before every append."""
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
        return
    current = (sheets.spreadsheets().values()
               .get(spreadsheetId=audit.SHEET_ID, range=f"{CALLS_TAB}!A1:Z1")
               .execute().get("values", [[]]))
    header = current[0] if current else []
    if header == CALLS_HEADERS:
        return
    if header and CALLS_HEADERS[:len(header)] == header:
        sheets.spreadsheets().values().update(
            spreadsheetId=audit.SHEET_ID, range=f"{CALLS_TAB}!A1",
            valueInputOption="RAW", body={"values": [CALLS_HEADERS]},
        ).execute()
    elif header:
        log.warning("claude_calls: header doesn't match an older or current "
                    "schema — leaving it alone: %r", header)


def ensure_prompts_folder(drive) -> str:
    """The Drive folder id for claude_prompts, creating it on first use."""
    resp = drive.files().list(
        q=(f"name = '{PROMPTS_FOLDER_NAME}' and "
           "mimeType = 'application/vnd.google-apps.folder' and trashed = false"),
        fields="files(id)", pageSize=1,
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    created = drive.files().create(
        body={"name": PROMPTS_FOLDER_NAME,
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return created["id"]


def upload_prompt(drive, folder_id: str, entry: Dict[str, Any]) -> Optional[str]:
    """Upload one call's full prompt as a .txt file; return its Drive link, or
    None on failure (a sync must not abort over one bad upload — see main)."""
    prompt = entry.get("prompt") or ""
    at = str(entry.get("at") or "unknown").replace(":", "-")
    source = str(entry.get("source") or "unknown")
    name = f"{at}_{source}.txt"
    try:
        created = drive.files().create(
            body={"name": name, "parents": [folder_id]},
            media_body=MediaIoBaseUpload(io.BytesIO(prompt.encode("utf-8")),
                                         mimetype="text/plain", resumable=False),
            fields="id,webViewLink",
        ).execute()
        return created.get("webViewLink")
    except Exception:
        log.warning("claude_calls: prompt upload failed for %s — link left "
                    "blank", name, exc_info=True)
        return None


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


def _build_row(entry: Dict[str, Any], drive_link: Optional[str]) -> List[Any]:
    prompt = entry.get("prompt") or ""
    preview = prompt[:PROMPT_PREVIEW_CHARS]
    if len(prompt) > PROMPT_PREVIEW_CHARS:
        preview += "…"
    return [
        entry.get("at"), entry.get("source"), entry.get("model"),
        entry.get("answered_by"), entry.get("effort"), entry.get("duration_s"),
        entry.get("status"), entry.get("cost_usd"), entry.get("error"),
        preview, len(prompt), drive_link or "",
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

    entries = []
    for raw in new_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning("claude_calls: skipping malformed log line: %r", raw[:200])

    if not entries:
        _write_synced_state(total)
        log.info("claude_calls: no parseable new calls (%d -> %d).", synced, total)
        return 0

    # Same fallback the audit job uses: the legacy combined token if this machine
    # already has one (it carries both scopes), otherwise the split credentials —
    # the service account for Sheets, the user's Drive token for the prompt files.
    legacy = audit.get_credentials()
    if legacy is not None:
        sheets_creds = drive_creds = legacy
    else:
        sheets_creds, drive_creds = audit.sheets_credentials(), audit.drive_credentials()
    sheets = build("sheets", "v4", credentials=sheets_creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=drive_creds, cache_discovery=False)

    folder_id = ensure_prompts_folder(drive)
    rows = [_build_row(entry, upload_prompt(drive, folder_id, entry))
            for entry in entries]

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
