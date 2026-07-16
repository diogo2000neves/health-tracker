"""Idempotent schema & dashboard maintenance. Run after schema changes:

    gcloud run jobs execute (job wrapping) python -m src.maintenance

* daily_summary — realigns the physical sheet to DAILY_HEADERS when a new
  column was added mid-table (inserts the column so existing data shifts
  correctly instead of being re-labelled under the wrong header).
* dashboard — creates the tab, stat labels and embedded charts (charts are
  API-defined, deliberately avoiding locale-sensitive formulas).
* insights — creates the tab the weekly AI summary appends to.

Runs as the Cloud Run runtime service account (ADC, Sheets scope). Safe to
re-run: every step is a no-op when already applied.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import google.auth
from googleapiclient.discovery import build

from src.presentation import (
    SCHEMA_HEADERS, collapse_requests, format_requests, header_note_requests,
    schema_legend, schema_rows,
)
from src.sheets import (
    DAILY_HEADERS, DAILY_TAB, DASHBOARD_FIRST_ROW, DASHBOARD_STATS, DASHBOARD_TAB,
    INSIGHTS_TAB, MEALS_TAB, READ_LAST_COL, SCHEMA_TAB, col_letter,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

INSIGHTS_HEADERS = ["week_ending", "insights", "model", "updated_at"]

# Target meals layout (mirror of ingest/main.py MEALS_HEADERS — kept here because
# maintenance runs in the daily image and can't import the standalone ingest).
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note", "template",
]

# Measured, reusable meals (mirror of ingest/main.py TEMPLATES_HEADERS).
TEMPLATES_TAB = "templates"
TEMPLATES_HEADERS = [
    "name", "description", "items", "portion_g",
    "calories", "protein_g", "carbs_g", "fat_g", "created_at", "updated_at",
]

# Column A: the title, a spacer, then one label per stat. run_daily.refresh_dashboard
# writes the values beside them in column B from the SAME list, so labels and
# numbers can't drift apart when a metric is added. (DASHBOARD_FIRST_ROW == 3 is
# the two header rows below.)
DASHBOARD_LABELS = [["HEALTH DASHBOARD"], [""]] + [
    [label] for label, _col, _kind in DASHBOARD_STATS
]
assert len(DASHBOARD_LABELS) == DASHBOARD_FIRST_ROW - 1 + len(DASHBOARD_STATS)


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


def _chart_requests(dashboard_id: int, daily_id: int) -> List[Dict[str, Any]]:
    """Two embedded charts sourcing daily_summary (open-ended row ranges)."""
    def col(idx: int) -> Dict[str, Any]:
        return {"sheetId": daily_id, "startRowIndex": 0,
                "startColumnIndex": idx, "endColumnIndex": idx + 1}

    def anchor(row: int) -> Dict[str, Any]:
        return {"overlayPosition": {
            "anchorCell": {"sheetId": dashboard_id, "rowIndex": row, "columnIndex": 3},
            "widthPixels": 860, "heightPixels": 320,
        }}

    def index(name: str) -> int:
        return DAILY_HEADERS.index(name)

    def series(name: str) -> Dict[str, Any]:
        return {"series": {"sourceRange": {"sources": [col(index(name))]}},
                "targetAxis": "LEFT_AXIS"}

    domain = [{"domain": {"sourceRange": {"sources": [col(index("date"))]}}}]

    line = {"addChart": {"chart": {
        "spec": {
            "title": "Weight & lean mass (kg)",
            "basicChart": {
                "chartType": "LINE", "legendPosition": "BOTTOM_LEGEND",
                "headerCount": 1, "domains": domain,
                "series": [series("weight_kg"), series("lean_mass_kg")],
            },
        },
        "position": anchor(1),
    }}}
    # Both sides of the energy balance on one chart: intake (meals) against
    # measured expenditure (Fitbit). The gap between the two lines is the whole
    # point of the system — a surplus or deficit you can actually see, rather than
    # a guess from weight alone.
    energy = {"addChart": {"chart": {
        "spec": {
            "title": "Energy balance (kcal/day): in vs out",
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "BOTTOM_LEGEND",
                "headerCount": 1, "domains": domain,
                "series": [series("total_cals_in"), series("total_cals_out")],
            },
        },
        "position": anchor(18),
    }}}
    # Sleep quality over time: the honest stand-in for the score the API doesn't
    # expose — how much of the night was actually spent restoring.
    sleep = {"addChart": {"chart": {
        "spec": {
            "title": "Sleep: deep & REM (mins)",
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "BOTTOM_LEGEND",
                "headerCount": 1, "domains": domain,
                "series": [series("sleep_deep_mins"), series("sleep_rem_mins")],
            },
        },
        "position": anchor(35),
    }}}
    return [line, energy, sleep]


def main() -> None:
    sid = os.environ["HEALTH_SPREADSHEET_ID"]
    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = svc.spreadsheets().get(
        spreadsheetId=sid,
        fields="sheets(properties(sheetId,title),charts(chartId))",
    ).execute()
    sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}

    # 1. daily_summary column alignment.
    message, layout_changed = _sync_daily_columns(
        svc, sid, sheets[DAILY_TAB]["properties"]["sheetId"])
    print(message)

    # 1b. meals column alignment (drop notes, add model, ...).
    if MEALS_TAB in sheets:
        print(_sync_meals_columns(svc, sid))

    # 2. dashboard tab + labels + charts.
    if DASHBOARD_TAB not in sheets:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": DASHBOARD_TAB}}}]},
        ).execute()
        meta = svc.spreadsheets().get(
            spreadsheetId=sid,
            fields="sheets(properties(sheetId,title),charts(chartId))",
        ).execute()
        sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
        print("dashboard: tab created")
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{DASHBOARD_TAB}!A1",
        valueInputOption="RAW", body={"values": DASHBOARD_LABELS},
    ).execute()
    # Charts are rebuilt whenever they can't be trusted: a realigned layout leaves
    # them plotting whatever moved into the old column indices, and a changed chart
    # count means the set itself was edited. Otherwise they're left alone, so the
    # user can drag/resize them without maintenance undoing it.
    wanted = _chart_requests(sheets[DASHBOARD_TAB]["properties"]["sheetId"],
                             sheets[DAILY_TAB]["properties"]["sheetId"])
    existing = [c["chartId"] for c in sheets[DASHBOARD_TAB].get("charts", [])]
    if layout_changed or len(existing) != len(wanted):
        try:
            requests = [{"deleteEmbeddedObject": {"objectId": cid}} for cid in existing]
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": requests + wanted}).execute()
            why = "layout moved" if layout_changed else "chart set changed"
            print(f"dashboard: rebuilt {len(wanted)} chart(s) ({why}, "
                  f"replaced {len(existing)})")
        except Exception as err:  # charts are cosmetic — never fail maintenance
            print(f"dashboard: chart rebuild skipped ({err})")
    else:
        print(f"dashboard: {len(existing)} chart(s) already in sync")

    # 3. insights tab.
    if INSIGHTS_TAB not in sheets:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": INSIGHTS_TAB}}}]},
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{INSIGHTS_TAB}!A1",
            valueInputOption="RAW", body={"values": [INSIGHTS_HEADERS]},
        ).execute()
        print("insights: tab created")
    else:
        print("insights: tab already present")

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

    Groups are created, then collapsed in a second pass (a group must exist before
    it can be collapsed). Re-running adds no duplicate groups: Sheets merges an
    identical range into the existing group."""
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": format_requests(daily_id)
                  + header_note_requests(daily_id)},
        ).execute()
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": collapse_requests(daily_id)},
        ).execute()
        return ("presentation: frozen panes, collapsible blocks, number formats "
                "and header notes applied")
    except Exception as err:  # cosmetic — never fail maintenance
        return f"presentation: skipped ({err})"


if __name__ == "__main__":
    main()
