"""One-off maintenance: retire the legacy `daily` tab and scrub `not food` test
rows from `meals`.

Runs as the Cloud Run runtime service account (ADC, Sheets scope) — the same
identity that writes the sheet, so it already has Editor rights. Idempotent:
re-running after a successful pass is a no-op.

    python -m src.cleanup
"""
from __future__ import annotations

import os

import google.auth
from googleapiclient.discovery import build

from src.sheets import MEALS_TAB

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
LEGACY_TAB = "daily"          # superseded by daily_summary
NOT_FOOD = "not food"          # ingest's tag for non-food photos


def main() -> None:
    sid = os.environ["HEALTH_SPREADSHEET_ID"]
    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    sheets = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}

    requests = []

    # 1. Retire the legacy `daily` tab.
    if LEGACY_TAB in sheets:
        requests.append({"deleteSheet": {"sheetId": sheets[LEGACY_TAB]["sheetId"]}})
        print(f"will delete tab '{LEGACY_TAB}' (sheetId {sheets[LEGACY_TAB]['sheetId']})")
    else:
        print(f"tab '{LEGACY_TAB}' already absent — skipping")

    # 2. Scrub `not food` rows from meals (bottom-up so grid indices stay valid).
    scrubbed = 0
    if MEALS_TAB in sheets:
        meals_id = sheets[MEALS_TAB]["sheetId"]
        values = (
            svc.spreadsheets().values()
            .get(spreadsheetId=sid, range=f"{MEALS_TAB}!A1:Z",
                 valueRenderOption="UNFORMATTED_VALUE")
            .execute().get("values", [])
        )
        if values:
            header = values[0]
            foods_col = header.index("foods") if "foods" in header else 2
            targets = [
                i  # 0-based grid row (row 0 is the header)
                for i, row in enumerate(values[1:], start=1)
                if str(row[foods_col] if foods_col < len(row) else "").strip().lower() == NOT_FOOD
            ]
            for i in sorted(targets, reverse=True):
                requests.append({"deleteDimension": {"range": {
                    "sheetId": meals_id, "dimension": "ROWS",
                    "startIndex": i, "endIndex": i + 1,
                }}})
            scrubbed = len(targets)
            print(f"will scrub {scrubbed} '{NOT_FOOD}' row(s) from '{MEALS_TAB}' at grid rows {targets}")

    if not requests:
        print("nothing to do.")
        return

    svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()
    print(f"done: retired '{LEGACY_TAB}' (if present) and scrubbed {scrubbed} "
          f"'{NOT_FOOD}' row(s) in spreadsheet {sid} (project {project}).")


if __name__ == "__main__":
    main()
