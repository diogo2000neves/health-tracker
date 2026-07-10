"""Cloud entry point: fetch weight/body-fat and upsert daily rows into a Sheet.

Designed to run as a Cloud Run Job. Reads:
  - HEALTH_OAUTH_TOKEN  : the user's OAuth token JSON (from Secret Manager) for
                          reading the Google Health API. Falls back to the local
                          credentials/token.json when unset.
  - HEALTH_SPREADSHEET_ID: target Google Sheet id (the Sheet must be shared with
                          the runtime service account).

Writes to the Sheet using the runtime service account (ADC), scoped to Sheets.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.google_health import BODY_FAT, WEIGHT, GoogleHealthClient
from src.sheets import SheetWriter

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Only record days on/after this date (YYYY-MM-DD). Set HEALTH_START_DATE=""
# to disable the cutoff and capture full history.
START_DATE = os.environ.get("HEALTH_START_DATE", "2026-07-04")


def load_user_credentials() -> Credentials:
    raw = os.environ.get("HEALTH_OAUTH_TOKEN")
    if raw:
        info = json.loads(raw)
    else:
        from src.auth import TOKEN_FILE  # local fallback for testing

        info = json.loads(TOKEN_FILE.read_text())
    creds = Credentials.from_authorized_user_info(info)
    creds.refresh(Request())
    return creds


def _first_of_day(points: List[Dict[str, Any]], key: str) -> Dict[str, Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """Map date -> (timestamp, metric, point) for the earliest reading each day."""
    best: Dict[str, Tuple[str, Dict[str, Any], Dict[str, Any]]] = {}
    for p in points:
        metric = p.get(key, {})
        ts = metric.get("sampleTime", {}).get("physicalTime", "")
        if not ts:
            continue
        day = ts[:10]
        if day not in best or ts < best[day][0]:
            best[day] = (ts, metric, p)
    return best


def daily_rows(weight_points: List[Dict[str, Any]], fat_points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    weights = _first_of_day(weight_points, "weight")
    fats = _first_of_day(fat_points, "bodyFat")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows: List[Dict[str, Any]] = []
    for day in sorted(set(weights) | set(fats)):
        if START_DATE and day < START_DATE:
            continue  # skip historical readings before the cutoff
        w = weights.get(day)
        f = fats.get(day)
        grams: Optional[float] = w[1].get("weightGrams") if w else None
        platform = (w or f)[2].get("dataSource", {}).get("platform")
        rows.append(
            {
                "date": day,
                "weight_kg": round(grams / 1000, 2) if isinstance(grams, (int, float)) else None,
                "body_fat_pct": f[1].get("percentage") if f else None,
                "source": platform,
                "updated_at": now,
            }
        )
    return rows


def main() -> None:
    spreadsheet_id = os.environ["HEALTH_SPREADSHEET_ID"]

    user_creds = load_user_credentials()
    client = GoogleHealthClient(user_creds)
    weight_points = client.list_data_points(WEIGHT)
    fat_points = client.list_data_points(BODY_FAT)
    rows = daily_rows(weight_points, fat_points)

    sa_creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    writer = SheetWriter(sa_creds, spreadsheet_id)
    writer.ensure_tab()
    writer.ensure_header()
    result = writer.upsert_rows(rows)

    print(
        f"Fetched {len(weight_points)} weight / {len(fat_points)} body-fat points; "
        f"built {len(rows)} daily rows; "
        f"appended {result['appended']}, updated {result['updated']} "
        f"in spreadsheet {spreadsheet_id} (project {project})."
    )


if __name__ == "__main__":
    main()
