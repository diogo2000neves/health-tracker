"""One-off migration to the revised two-table schema.

* meals: drop the redundant `date` column and add an `items` JSON column. Legacy
  rows (a single aggregate food) are converted into a one-item breakdown so no
  information is lost.
* intraday_events: delete the tab entirely — the model is now just `daily_summary`
  + `meals`, and each day's representative weigh-in already lives in daily_summary.

Runs as the Cloud Run runtime service account (ADC, Sheets scope). Idempotent:
guarded so a second run is a no-op.

    python -m src.migrate_schema
"""
from __future__ import annotations

import json
import os

import google.auth
from googleapiclient.discovery import build

from src.sheets import MEALS_TAB

LEGACY_INTRADAY_TAB = "intraday_events"

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Target meals layout (must match ingest/main.py MEALS_HEADERS).
NEW_MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "photo_url",
    "portion_g", "notes",
]
NOT_FOOD = "not food"


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _reshape_meals(svc, sid: str) -> None:
    values = (
        svc.spreadsheets().values()
        .get(spreadsheetId=sid, range=f"{MEALS_TAB}!A1:Z",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )
    if not values:
        print("meals: empty — skipping")
        return
    header = values[0]
    if "date" not in header:  # already migrated
        print("meals: already new schema — skipping")
        return

    rows = [dict(zip(header, r)) for r in values[1:]]
    new_rows = []
    for r in rows:
        foods = str(r.get("foods", "") or "")
        is_food = foods.strip().lower() != NOT_FOOD and _num(r.get("calories")) > 0
        items = [{
            "name": foods,
            "portion_g": _num(r.get("portion_g")),
            "calories": _num(r.get("calories")),
            "protein_g": _num(r.get("protein_g")),
            "carbs_g": _num(r.get("carbs_g")),
            "fat_g": _num(r.get("fat_g")),
        }] if is_food else []
        new_rows.append([
            r.get("datetime", "") or "",
            foods,
            json.dumps(items, ensure_ascii=False),
            _num(r.get("calories")), _num(r.get("protein_g")),
            _num(r.get("carbs_g")), _num(r.get("fat_g")),
            _num(r.get("confidence")), r.get("photo_url", "") or "",
            _num(r.get("portion_g")), r.get("notes", "") or "",
        ])

    svc.spreadsheets().values().clear(
        spreadsheetId=sid, range=f"{MEALS_TAB}!A1:Z"
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"{MEALS_TAB}!A1",
        valueInputOption="RAW", body={"values": [NEW_MEALS_HEADERS] + new_rows},
    ).execute()
    print(f"meals: reshaped {len(new_rows)} row(s) to new schema (dropped `date`, added `items`)")


def main() -> None:
    sid = os.environ["HEALTH_SPREADSHEET_ID"]
    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    sheets = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}

    if MEALS_TAB in sheets:
        _reshape_meals(svc, sid)

    if LEGACY_INTRADAY_TAB in sheets:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"deleteSheet": {
                "sheetId": sheets[LEGACY_INTRADAY_TAB]["sheetId"]}}]},
        ).execute()
        print(f"deleted tab '{LEGACY_INTRADAY_TAB}'")
    else:
        print(f"tab '{LEGACY_INTRADAY_TAB}' already absent — skipping")

    print(f"migration done (spreadsheet {sid}, project {project}).")


if __name__ == "__main__":
    main()
