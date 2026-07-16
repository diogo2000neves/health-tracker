"""Idempotent schema & dashboard maintenance. Run after schema changes:

    gcloud run jobs execute (job wrapping) python -m src.maintenance

* daily_summary — realigns the physical sheet to DAILY_HEADERS when a new
  column was added mid-table (inserts the column so existing data shifts
  correctly instead of being re-labelled under the wrong header).

Runs as the Cloud Run runtime service account (ADC, Sheets scope). Safe to
re-run: every step is a no-op when already applied.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import google.auth
from googleapiclient.discovery import build

from src.presentation import (
    SCHEMA_HEADERS, block_groups, clear_group_requests, collapse_requests,
    format_requests, header_note_requests, schema_legend, schema_rows,
)
from src.sheets import (
    DAILY_HEADERS, DAILY_TAB, MEALS_TAB, READ_LAST_COL, SCHEMA_TAB, col_letter,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note", "template",
]

TEMPLATES_TAB = "templates"
TEMPLATES_HEADERS = [
    "name", "description", "items", "portion_g",
    "calories", "protein_g", "carbs_g", "fat_g", "created_at", "updated_at",
]



def _sync_daily_columns(svc, sid: str, daily_id: int) -> Tuple[str, bool]:
    """Realign daily_summary to DAILY_HEADERS **by header name**, preserving every
    row's data. Returns (message, changed).

    Rebuilds the tab rather than inserting columns in place, because that is the
    only approach that survives all three kinds of schema change at once: adding a
    column, dropping one, and *reordering*. (The old insert-only version wrote the
    new header over row 1 at the end, which silently re-labelled every value when
    the order differed — the exact corruption the "never reorder" rule existed to
    dodge.)

    Refuses to drop a column that still holds data: losing history to a schema edit
    must be a deliberate act, not a side effect of running maintenance.
    """
    values = (
        svc.spreadsheets().values()
        .get(spreadsheetId=sid, range=f"{DAILY_TAB}!A1:{READ_LAST_COL}",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )
    if not values:
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{DAILY_TAB}!A1",
            valueInputOption="RAW", body={"values": [DAILY_HEADERS]},
        ).execute()
        return "daily_summary: empty — header written", True

    header = values[0]
    if header == DAILY_HEADERS:
        return "daily_summary: header already in sync", False

    rows = [dict(zip(header, row)) for row in values[1:]]
    dropped = [h for h in header if h and h not in DAILY_HEADERS]
    for name in dropped:
        holding = [r[name] for r in rows if str(r.get(name, "")).strip() != ""]
        if holding:
            raise RuntimeError(
                f"refusing to drop {DAILY_TAB} column {name!r}: it still holds "
                f"{len(holding)} value(s), e.g. {holding[:3]}. Migrate the data "
                "first, then remove the column from DAILY_HEADERS."
            )

    body = [DAILY_HEADERS] + [
        [("" if r.get(h) is None else r.get(h, "")) for h in DAILY_HEADERS]
        for r in rows
    ]
    svc.spreadsheets().values().clear(
        spreadsheetId=sid, range=f"{DAILY_TAB}!A1:{READ_LAST_COL}").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{DAILY_TAB}!A1",
        valueInputOption="RAW", body={"values": body}).execute()

    added = [h for h in DAILY_HEADERS if h not in header]
    return (f"daily_summary: realigned {len(rows)} row(s) to {len(DAILY_HEADERS)} "
            f"columns (+{len(added)} added, -{len(dropped)} dropped: {dropped})"), True


def _sync_meals_columns(svc, sid: str) -> str:
    """Realign the meals tab to MEALS_HEADERS in place — dropping removed columns
    (e.g. `notes`) and adding new ones blank (e.g. `model`) — while preserving
    every existing row's data by header name. Idempotent (no-op when in sync)."""
    values = (
        svc.spreadsheets().values()
        .get(spreadsheetId=sid, range=f"{MEALS_TAB}!A1:Z",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )
    if not values:
        return "meals: empty — skipping"
    header = values[0]
    if header == MEALS_HEADERS:
        return "meals: header already in sync"
    rows = [dict(zip(header, r)) for r in values[1:]]
    body = [MEALS_HEADERS] + [
        [("" if r.get(h) is None else r.get(h, "")) for h in MEALS_HEADERS]
        for r in rows
    ]
    svc.spreadsheets().values().clear(
        spreadsheetId=sid, range=f"{MEALS_TAB}!A1:Z").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{MEALS_TAB}!A1",
        valueInputOption="RAW", body={"values": body}).execute()
    return f"meals: realigned {len(rows)} row(s) to {len(MEALS_HEADERS)} columns"



def main() -> None:
    sid = os.environ["HEALTH_SPREADSHEET_ID"]
    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId,title),charts(chartId))",
    ).execute()
    sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}

    # Delete obsolete tabs if present.
    for obsolete in ["analysis", "dashboard", "insights"]:
        if obsolete in sheets:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"deleteSheet": {"sheetId": sheets[obsolete]["properties"]["sheetId"]}}]},
            ).execute()
            print(f"{obsolete}: obsolete tab deleted")
            del sheets[obsolete]

    # 1. daily_summary column alignment.
    message, layout_changed = _sync_daily_columns(
        svc, sid, sheets[DAILY_TAB]["properties"]["sheetId"])
    print(message)

    # 1b. meals column alignment (drop notes, add model, ...).
    if MEALS_TAB in sheets:
        print(_sync_meals_columns(svc, sid))



    # 4. templates tab (measured, reusable meals; written by the ingest service).
    if TEMPLATES_TAB not in sheets:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": TEMPLATES_TAB}}}]},
        ).execute()
        print("templates: tab created")
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{TEMPLATES_TAB}!A1",
        valueInputOption="RAW", body={"values": [TEMPLATES_HEADERS]},
    ).execute()
    print("templates: header in sync")

    # 5. schema tab — the data dictionary, regenerated from the registry.
    print(_sync_schema_tab(svc, sid, sheets))

    # 6. presentation: frozen panes, collapsible blocks, units, header notes.
    print(_apply_presentation(svc, sid, sheets[DAILY_TAB]["properties"]["sheetId"]))

    print(f"maintenance done (spreadsheet {sid}, project {project}).")


def _sync_schema_tab(svc, sid: str, sheets: Dict[str, Any]) -> str:
    """(Re)write the `schema` tab: what every column means, in the sheet itself.

    Kept in the spreadsheet rather than only in the repo so that anything reading
    the data — an AI agent, the iOS app, future me — gets the dictionary in the
    same place as the numbers, with no second system to go look up."""
    if SCHEMA_TAB not in sheets:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": SCHEMA_TAB}}}]},
        ).execute()
    body = [schema_legend(), SCHEMA_HEADERS] + schema_rows()
    svc.spreadsheets().values().clear(spreadsheetId=sid, range=SCHEMA_TAB).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{SCHEMA_TAB}!A1",
        valueInputOption="RAW", body={"values": body},
    ).execute()
    return f"schema: documented {len(schema_rows())} column(s)"


def _apply_presentation(svc, sid: str, daily_id: int) -> str:
    """Formatting only — never touches a value, so it is always safe to re-run.

    Three passes, in this order for real reasons: existing groups are deleted first
    (they're positional, so a schema reorder leaves them spanning the wrong
    columns), then rebuilt, then collapsed — a group must exist before it can be
    collapsed, and it must be created in its own batch to be visible to the next.
    """
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sid,
            fields="sheets(properties(sheetId),columnGroups(range,depth))",
        ).execute()
        existing: List[Dict[str, Any]] = []
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["sheetId"] == daily_id:
                existing = sheet.get("columnGroups", [])
        if existing:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": clear_group_requests(daily_id, existing)},
            ).execute()

        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": format_requests(daily_id)
                  + header_note_requests(daily_id)},
        ).execute()
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": collapse_requests(daily_id)},
        ).execute()
        return (f"presentation: {len(block_groups())} collapsible block(s) "
                f"(replaced {len(existing)}), frozen panes, number formats, "
                "header notes")
    except Exception as err:  # cosmetic — never fail maintenance
        return f"presentation: skipped ({err})"


if __name__ == "__main__":
    main()
