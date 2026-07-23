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
    "portion_g", "image_sha", "note", "template", "edited_at",
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

    # 6. drop physical columns the schema no longer has (empty ones only), then
    #    apply presentation. Order matters: trimming shifts column indices, so the
    #    groups must be rebuilt afterwards or they'd span the wrong columns.
    daily_id = sheets[DAILY_TAB]["properties"]["sheetId"]
    print(_trim_stale_columns(svc, sid, daily_id))
    print(_apply_presentation(svc, sid, daily_id))

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


# Column groups can nest, and each delete pass only strips one level. Six is far
# more depth than this sheet will ever have; the loop exists to terminate, not to
# iterate.
_MAX_GROUP_PASSES = 6


def _column_groups(svc, sid: str, daily_id: int) -> List[Dict[str, Any]]:
    """The column groups currently on the tab, straight from the API."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId),columnGroups(range,depth,collapsed))",
    ).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] == daily_id:
            return sheet.get("columnGroups", []) or []
    return []


def _hidden_columns(svc, sid: str, daily_id: int) -> set:
    """Indices of columns the sheet is currently hiding.

    `hiddenByUser` belongs to the column, not to any group, so it survives the
    group that hid it. Read back explicitly — a correct set of groups over
    still-hidden columns looks exactly like a broken sheet.
    """
    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId),data(columnMetadata(hiddenByUser)))",
    ).execute()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] != daily_id:
            continue
        data = sheet.get("data") or [{}]
        columns = data[0].get("columnMetadata") or []
        return {i for i, c in enumerate(columns) if c.get("hiddenByUser")}
    return set()


def _flatten_groups(svc, sid: str, daily_id: int) -> int:
    """Remove EVERY column group, re-reading until the sheet agrees none are left.

    `deleteDimensionGroup` decrements the depth of the dimensions in a range rather
    than removing a group object, so one pass cannot clear nested groups. The old
    code deleted once and then reported success using the count it had read
    *before* deleting — so it printed "replaced 5" on three consecutive runs while
    the sheet stayed broken. Never trust a write you haven't read back.
    """
    removed = 0
    for _ in range(_MAX_GROUP_PASSES):
        groups = _column_groups(svc, sid, daily_id)
        if not groups:
            return removed
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": clear_group_requests(daily_id, groups)},
        ).execute()
        removed += len(groups)
    left = _column_groups(svc, sid, daily_id)
    if left:
        raise RuntimeError(
            f"could not flatten column groups: {len(left)} still on the sheet "
            f"after {_MAX_GROUP_PASSES} passes ({left[:2]})")
    return removed


def _trim_stale_columns(svc, sid: str, daily_id: int) -> str:
    """Delete grid columns past the end of the schema.

    Shrinking the schema leaves the physical column behind: dropping
    `subjective_feel` took the header from 79 to 78, and the realign blanked the
    79th but could not remove it — that is the stray empty `CA` at the end of the
    sheet. Only deletes columns that are both outside DAILY_HEADERS *and* empty, so
    this can never eat data.
    """
    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId,gridProperties(columnCount)))",
    ).execute()
    count = 0
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] == daily_id:
            count = sheet["properties"]["gridProperties"]["columnCount"]
    want = len(DAILY_HEADERS)
    if count <= want:
        return f"columns: {count} — no stale columns past the schema"

    first, last = col_letter(want), col_letter(count - 1)
    values = (
        svc.spreadsheets().values()
        .get(spreadsheetId=sid, range=f"{DAILY_TAB}!{first}:{last}")
        .execute().get("values", [])
    )
    if any(str(cell).strip() for row in values for cell in row):
        return (f"columns: {count - want} past the schema ({first}:{last}) still "
                "hold data — left alone, migrate them first")

    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [{"deleteDimension": {"range": {
            "sheetId": daily_id, "dimension": "COLUMNS",
            "startIndex": want, "endIndex": count,
        }}}]},
    ).execute()
    return f"columns: deleted {count - want} empty column(s) past the schema ({first}:{last})"


def _apply_presentation(svc, sid: str, daily_id: int) -> str:
    """Formatting only — never touches a value, so it is always safe to re-run.

    Flatten, rebuild, collapse, then **verify against the sheet**. Each step is its
    own batch because a group must exist before it can be collapsed, and the final
    read-back is the point: this function's whole failure mode was reporting a
    success it never checked.
    """
    try:
        removed = _flatten_groups(svc, sid, daily_id)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": format_requests(daily_id)
                  + header_note_requests(daily_id)},
        ).execute()
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": collapse_requests(daily_id)},
        ).execute()

        want = block_groups()
        final = _column_groups(svc, sid, daily_id)
        if len(final) != len(want):
            return (f"presentation: BROKEN — wanted {len(want)} groups, the sheet "
                    f"has {len(final)}. Ranges: "
                    f"{[(g.get('range', {}).get('startIndex', 0), g.get('range', {}).get('endIndex')) for g in final]}")

        # Groups being right is not the same as the sheet LOOKING right: hidden-ness
        # is a column property that outlives the group that caused it. Check what is
        # actually visible, which is what the user sees.
        hidden = _hidden_columns(svc, sid, daily_id)
        grouped = {i for g in want for i in range(g["start"], g["end"])}
        anchors = [i for i in range(len(DAILY_HEADERS)) if i not in grouped]
        stuck = [DAILY_HEADERS[i] for i in anchors if i in hidden]
        if stuck:
            return (f"presentation: BROKEN — {len(stuck)} anchor column(s) still "
                    f"hidden despite no group covering them: {stuck}")
        return (f"presentation: {len(want)} collapsible block(s) verified "
                f"(removed {removed} stale); visible when collapsed: "
                f"{[DAILY_HEADERS[i] for i in anchors]}")
    except Exception as err:  # cosmetic — must never fail the data migration
        return f"presentation: FAILED ({err})"


if __name__ == "__main__":
    main()
