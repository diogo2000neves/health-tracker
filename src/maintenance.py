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
from typing import Any, Dict, List

import google.auth
from googleapiclient.discovery import build

from src.sheets import (
    DAILY_HEADERS, DAILY_TAB, DASHBOARD_TAB, INSIGHTS_TAB, MEALS_TAB, col_letter,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

INSIGHTS_HEADERS = ["week_ending", "insights", "model", "updated_at"]

# Target meals layout (mirror of ingest/main.py MEALS_HEADERS — kept here because
# maintenance runs in the daily image and can't import the standalone ingest).
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note",
]

DASHBOARD_LABELS = [
    ["HEALTH DASHBOARD"],
    [""],
    ["Latest weight (kg)"],
    ["Latest body fat (%)"],
    ["Latest lean mass (kg)"],
    ["Avg kcal (last 7 logged days)"],
    ["Avg protein g (7d)"],
    ["Avg carbs g (7d)"],
    ["Avg fat g (7d)"],
    ["Nutrition days in window"],
    ["Stats updated (UTC)"],
]


def _sync_daily_columns(svc, sid: str, daily_id: int) -> str:
    """Insert any column DAILY_HEADERS defines that the sheet lacks (in place)."""
    header = (
        svc.spreadsheets().values()
        .get(spreadsheetId=sid, range=f"{DAILY_TAB}!1:1")
        .execute().get("values", [[]])
    )[0]
    missing = [h for h in DAILY_HEADERS if h not in header]
    if not missing:
        return "daily_summary: header already in sync"
    for name in missing:
        target = DAILY_HEADERS.index(name)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"insertDimension": {
                "range": {"sheetId": daily_id, "dimension": "COLUMNS",
                          "startIndex": target, "endIndex": target + 1},
                "inheritFromBefore": False,
            }}]},
        ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{DAILY_TAB}!A1",
        valueInputOption="RAW", body={"values": [DAILY_HEADERS]},
    ).execute()
    return f"daily_summary: inserted column(s) {missing}"


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

    date_i = DAILY_HEADERS.index("date")
    weight_i = DAILY_HEADERS.index("weight_kg")
    lean_i = DAILY_HEADERS.index("lean_mass_kg")
    cals_i = DAILY_HEADERS.index("total_cals_in")

    def series(idx: int) -> Dict[str, Any]:
        return {"series": {"sourceRange": {"sources": [col(idx)]}},
                "targetAxis": "LEFT_AXIS"}

    line = {"addChart": {"chart": {
        "spec": {
            "title": "Weight & lean mass (kg)",
            "basicChart": {
                "chartType": "LINE", "legendPosition": "BOTTOM_LEGEND",
                "headerCount": 1,
                "domains": [{"domain": {"sourceRange": {"sources": [col(date_i)]}}}],
                "series": [series(weight_i), series(lean_i)],
            },
        },
        "position": anchor(1),
    }}}
    bars = {"addChart": {"chart": {
        "spec": {
            "title": "Calories in (kcal/day)",
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "NO_LEGEND",
                "headerCount": 1,
                "domains": [{"domain": {"sourceRange": {"sources": [col(date_i)]}}}],
                "series": [series(cals_i)],
            },
        },
        "position": anchor(18),
    }}}
    return [line, bars]


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
    print(_sync_daily_columns(svc, sid, sheets[DAILY_TAB]["properties"]["sheetId"]))

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
    if not sheets[DASHBOARD_TAB].get("charts"):
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": _chart_requests(
                    sheets[DASHBOARD_TAB]["properties"]["sheetId"],
                    sheets[DAILY_TAB]["properties"]["sheetId"],
                )},
            ).execute()
            print("dashboard: charts created")
        except Exception as err:  # charts are cosmetic — never fail maintenance
            print(f"dashboard: chart creation skipped ({err})")
    else:
        print("dashboard: charts already present")

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

    print(f"maintenance done (spreadsheet {sid}, project {project}).")


if __name__ == "__main__":
    main()
