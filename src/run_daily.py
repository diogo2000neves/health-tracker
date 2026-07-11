"""Cloud entry point: refresh the per-day health model for a trailing window.

Each run (Cloud Run Job, triggered ~07:00 Europe/Lisbon):

  1. Pulls weight & body-fat from the Google Health API and rolls each day's
     representative (earliest/morning) reading into ``daily_summary`` as physique
     columns.
  2. Rolls meals logged in the ``meals`` tab into ``daily_summary`` nutrition
     totals per day; non-food shots are ignored.

Only the trailing ``HEALTH_RECONCILE_DAYS`` days are re-rolled, so *yesterday* is
finalised once its data has fully landed (late Fitbit sync, late meal logs) while
*today* keeps updating. Set that to 0 for a one-off full backfill/reconstruction
from source.

Readiness columns (sleep/HRV/SpO2/skin-temp/steps/active-mins) are intentionally
left blank here; the Fitbit biometrics step (Task 2) fills them through the same
merge-upsert, so this job never clobbers them.

Reads HEALTH_OAUTH_TOKEN (Secret Manager) for the Health API; writes the Sheet
with the runtime service account (ADC, Sheets scope).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.google_health import BODY_FAT, WEIGHT, GoogleHealthClient
from src.sheets import DAILY_HEADERS, DAILY_TAB, MEALS_TAB, SheetClient

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Floor: never record days before this (YYYY-MM-DD). "" disables the floor.
START_DATE = os.environ.get("HEALTH_START_DATE", "2026-07-04")
# Re-roll this many trailing days each run (catches late sync / late meal logs).
# 0 => unbounded (full backfill / reconstruction from immutable sources).
RECONCILE_DAYS = int(os.environ.get("HEALTH_RECONCILE_DAYS", "7"))

# Ingest tags non-food photos with this; such rows contribute nothing to totals.
NOT_FOOD = "not food"


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


def window_start() -> Optional[str]:
    """Earliest date (YYYY-MM-DD) to (re)roll, combining the two bounds."""
    bounds: List[str] = []
    if START_DATE:
        bounds.append(START_DATE)
    if RECONCILE_DAYS > 0:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=RECONCILE_DAYS)
        bounds.append(cutoff.isoformat())
    return max(bounds) if bounds else None


def _in_window(day: str, start: Optional[str]) -> bool:
    return start is None or day >= start


def _sample_time(metric: Dict[str, Any]) -> str:
    """RFC3339 timestamp (UTC 'Z') of a reading, or '' when absent."""
    return metric.get("sampleTime", {}).get("physicalTime", "")


def _weight_kg(metric: Dict[str, Any]) -> Optional[float]:
    grams = metric.get("weightGrams")
    return round(grams / 1000, 2) if isinstance(grams, (int, float)) else None


# -- extraction -------------------------------------------------------------
def daily_body(
    weight_points: List[Dict[str, Any]],
    fat_points: List[Dict[str, Any]],
    start: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """date -> {weight_kg, body_fat_pct} from the earliest (morning) reading."""

    def first_of_day(points, metric_key, value_fn) -> Dict[str, Any]:
        best: Dict[str, Tuple[str, Any]] = {}
        for point in points:
            metric = point.get(metric_key, {})
            ts = _sample_time(metric)
            value = value_fn(metric)
            if not ts or value is None or not _in_window(ts[:10], start):
                continue
            day = ts[:10]
            if day not in best or ts < best[day][0]:
                best[day] = (ts, value)
        return {day: value for day, (_, value) in best.items()}

    weights = first_of_day(weight_points, "weight", _weight_kg)
    fats = first_of_day(fat_points, "bodyFat", lambda m: m.get("percentage"))
    return {
        day: {"weight_kg": weights.get(day), "body_fat_pct": fats.get(day)}
        for day in set(weights) | set(fats)
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def daily_nutrition(meals: List[Dict[str, str]], start: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Sum meal macros per day, ignoring non-food test shots."""
    fields = {
        "total_cals_in": "calories",
        "total_protein_g": "protein_g",
        "total_carbs_g": "carbs_g",
        "total_fat_g": "fat_g",
    }
    totals: Dict[str, Dict[str, float]] = defaultdict(lambda: {k: 0.0 for k in fields})
    for meal in meals:
        # `date` was dropped from the meals schema; derive the day from datetime.
        day = (meal.get("datetime") or "")[:10]
        if not day or not _in_window(day, start):
            continue
        if (meal.get("foods") or "").strip().lower() == NOT_FOOD:
            continue
        for out_key, meal_key in fields.items():
            totals[day][out_key] += _num(meal.get(meal_key))
    return {day: {k: round(v, 1) for k, v in vals.items()} for day, vals in totals.items()}


def build_daily_rows(
    body: Dict[str, Dict[str, Any]],
    nutrition: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One merge-row per day that has body and/or nutrition data."""
    rows: List[Dict[str, Any]] = []
    for day in sorted(set(body) | set(nutrition)):
        row: Dict[str, Any] = {"date": day}
        row.update(body.get(day, {}))
        row.update(nutrition.get(day, {}))
        rows.append(row)
    return rows


def main() -> None:
    spreadsheet_id = os.environ["HEALTH_SPREADSHEET_ID"]
    start = window_start()

    user_creds = load_user_credentials()
    health = GoogleHealthClient(user_creds)
    weight_points = health.list_data_points(WEIGHT)
    fat_points = health.list_data_points(BODY_FAT)

    sa_creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    sheet = SheetClient(sa_creds, spreadsheet_id)
    sheet.ensure_tab(DAILY_TAB, DAILY_HEADERS)

    body = daily_body(weight_points, fat_points, start)
    meals = sheet.read_rows(MEALS_TAB)
    nutrition = daily_nutrition(meals, start)
    daily_result = sheet.upsert_daily(build_daily_rows(body, nutrition))

    print(
        f"window>={start or 'ALL'}: fetched {len(weight_points)} weight / "
        f"{len(fat_points)} body-fat points; read {len(meals)} meal rows; "
        f"daily_summary updated {daily_result['updated']}, appended "
        f"{daily_result['appended']} in spreadsheet {spreadsheet_id} "
        f"(project {project})."
    )


if __name__ == "__main__":
    main()
