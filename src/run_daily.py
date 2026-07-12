"""Cloud entry point: refresh the per-day health model for a trailing window.

Each run (Cloud Run Job, triggered ~07:00 Europe/Lisbon):

  1. Pulls weight & body-fat from the Google Health API and rolls each day's
     representative (earliest) reading into ``daily_summary`` as physique
     columns, including derived ``lean_mass_kg``.
  2. Rolls meals logged in the ``meals`` tab into ``daily_summary`` nutrition
     totals per day; non-food shots and failed analyses are ignored.
  3. Refreshes the ``dashboard`` tab's stat cells (best-effort).

Day grain — the one rule that keeps every join honest: a reading belongs to the
**local civil day** it happened on (from the API's ``civilTime``/``utcOffset``),
matching the local-time day already used by ``meals``. Never the UTC day.

Only the trailing ``HEALTH_RECONCILE_DAYS`` days are re-rolled, so *yesterday*
is finalised once its data has fully landed while *today* keeps updating. Set
it to 0 for a one-off full backfill/reconstruction from source.

Readiness columns (sleep/HRV/SpO2/skin-temp/steps/active-mins) are left blank
here; the Fitbit biometrics step fills them through the same merge-upsert.

Reads HEALTH_OAUTH_TOKEN (Secret Manager; health-only scopes) for the Health
API; writes the Sheet with the runtime service account (ADC, Sheets scope).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import google.auth
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.google_health import BODY_FAT, WEIGHT, GoogleHealthClient
from src.sheets import (
    DAILY_HEADERS, DAILY_TAB, DASHBOARD_TAB, MEALS_TAB, SheetClient, TIER1_NUTRIENTS,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Meal rows excluded from nutrition totals (kept in sync with ingest/main.py).
NON_MEALS = {"not food", "analysis failed"}


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


def window_start(start_date: str, reconcile_days: int,
                 today: Optional[date] = None) -> Optional[str]:
    """Earliest date (YYYY-MM-DD) to (re)roll, combining the two bounds.

    `start_date` is the hard floor ("" disables); `reconcile_days` is the
    trailing re-roll window (0 = unbounded, for full backfills).
    """
    bounds: List[str] = []
    if start_date:
        bounds.append(start_date)
    if reconcile_days > 0:
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=reconcile_days)
        bounds.append(cutoff.isoformat())
    return max(bounds) if bounds else None


def _in_window(day: str, start: Optional[str]) -> bool:
    return start is None or day >= start


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# -- Google Health payload helpers -------------------------------------------
def _physical_time(metric: Dict[str, Any]) -> str:
    """RFC3339 UTC timestamp of a reading (for ordering), or '' when absent."""
    return metric.get("sampleTime", {}).get("physicalTime", "")


def _local_date(metric: Dict[str, Any]) -> str:
    """Local civil date (YYYY-MM-DD) of a reading.

    Prefers the API's explicit civilTime; falls back to physicalTime shifted by
    utcOffset; last resort is the UTC date. This is what keeps a 00:30 local
    weigh-in on the correct day.
    """
    st = metric.get("sampleTime", {})
    civil = st.get("civilTime", {}).get("date", {})
    if all(k in civil for k in ("year", "month", "day")):
        return f"{civil['year']:04d}-{civil['month']:02d}-{civil['day']:02d}"
    ts = st.get("physicalTime", "")
    if not ts:
        return ""
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    try:
        offset_s = int(str(st.get("utcOffset", "0s")).rstrip("s") or 0)
    except ValueError:
        offset_s = 0
    return (moment + timedelta(seconds=offset_s)).date().isoformat()


def _weight_kg(metric: Dict[str, Any]) -> Optional[float]:
    grams = metric.get("weightGrams")
    return round(grams / 1000, 2) if isinstance(grams, (int, float)) else None


def _body_fat_pct(metric: Dict[str, Any]) -> Optional[float]:
    pct = metric.get("percentage")
    return pct if isinstance(pct, (int, float)) else None


# -- extraction ---------------------------------------------------------------
def daily_body(
    weight_points: List[Dict[str, Any]],
    fat_points: List[Dict[str, Any]],
    start: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """local date -> {weight_kg, body_fat_pct} from each day's earliest reading."""

    def first_of_day(points: List[Dict[str, Any]], key: str, value_fn) -> Dict[str, Any]:
        best: Dict[str, tuple] = {}
        for point in points:
            metric = point.get(key, {})
            day = _local_date(metric)
            ts = _physical_time(metric)
            value = value_fn(metric)
            if not day or not ts or value is None or not _in_window(day, start):
                continue
            if day not in best or ts < best[day][0]:
                best[day] = (ts, value)
        return {day: value for day, (_, value) in best.items()}

    weights = first_of_day(weight_points, "weight", _weight_kg)
    fats = first_of_day(fat_points, "bodyFat", _body_fat_pct)
    return {
        day: {"weight_kg": weights.get(day), "body_fat_pct": fats.get(day)}
        for day in set(weights) | set(fats)
    }


def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    """The meals `items` cell holds a JSON array of per-ingredient objects."""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def daily_nutrition(meals: List[Dict[str, Any]], start: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Per local day: sum macros (flat meal columns) and Tier-1 micronutrients
    (from each meal's per-ingredient `items` JSON). Non-food and zero-content
    rows are ignored. Nutrient totals are emitted only when non-zero, so days
    before nutrient tracking stay blank rather than showing misleading zeros."""
    macro_fields = {
        "total_cals_in": "calories",
        "total_protein_g": "protein_g",
        "total_carbs_g": "carbs_g",
        "total_fat_g": "fat_g",
    }

    def _blank() -> Dict[str, float]:
        vals = {k: 0.0 for k in macro_fields}
        vals.update({f"total_{n}": 0.0 for n in TIER1_NUTRIENTS})
        return vals

    totals: Dict[str, Dict[str, float]] = defaultdict(_blank)
    for meal in meals:
        day = str(meal.get("datetime") or "")[:10]
        if not day or not _in_window(day, start):
            continue
        if str(meal.get("foods") or "").strip().lower() in NON_MEALS:
            continue
        macros = {k: _num(meal.get(mk)) for k, mk in macro_fields.items()}
        if max(macros.values()) <= 0:
            continue  # zero-content rows carry no signal
        for k, v in macros.items():
            totals[day][k] += v
        for item in _parse_items(meal.get("items")):
            nutrients = item.get("nutrients") or {}
            for n in TIER1_NUTRIENTS:
                totals[day][f"total_{n}"] += _num(nutrients.get(n))

    out: Dict[str, Dict[str, Any]] = {}
    for day, vals in totals.items():
        out[day] = {
            k: round(v, 2)
            for k, v in vals.items()
            if k in macro_fields or v > 0  # macros always; nutrients only if present
        }
    return out


def build_daily_rows(
    body: Dict[str, Dict[str, Any]],
    nutrition: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One merge-row per day; derives lean mass when both inputs exist."""
    rows: List[Dict[str, Any]] = []
    for day in sorted(set(body) | set(nutrition)):
        row: Dict[str, Any] = {"date": day}
        row.update(body.get(day, {}))
        row.update(nutrition.get(day, {}))
        weight, fat = row.get("weight_kg"), row.get("body_fat_pct")
        if isinstance(weight, (int, float)) and isinstance(fat, (int, float)):
            row["lean_mass_kg"] = round(weight * (1 - fat / 100), 2)
        rows.append(row)
    return rows


# -- dashboard -----------------------------------------------------------------
def refresh_dashboard(sheet: SheetClient) -> None:
    """Rewrite the dashboard stat cells (B3:B11). No-op if the tab is absent."""
    if DASHBOARD_TAB not in sheet.tab_titles():
        return
    rows = sorted(sheet.read_rows(DAILY_TAB), key=lambda r: str(r.get("date", "")))

    def latest(col: str) -> Any:
        for row in reversed(rows):
            value = row.get(col)
            if isinstance(value, (int, float)):
                return value
        return ""

    logged = [r for r in rows if _num(r.get("total_cals_in")) > 0][-7:]

    def avg(col: str, digits: int = 0) -> Any:
        if not logged:
            return ""
        return round(sum(_num(r.get(col)) for r in logged) / len(logged), digits)

    stats = [
        [latest("weight_kg")],
        [latest("body_fat_pct")],
        [latest("lean_mass_kg")],
        [avg("total_cals_in")],
        [avg("total_protein_g")],
        [avg("total_carbs_g")],
        [avg("total_fat_g")],
        [len(logged)],
        [datetime.now(timezone.utc).isoformat(timespec="seconds")],
    ]
    sheet.write_values(DASHBOARD_TAB, "B3", stats)


# -- entry point ----------------------------------------------------------------
def main() -> None:
    spreadsheet_id = os.environ["HEALTH_SPREADSHEET_ID"]
    start = window_start(
        os.environ.get("HEALTH_START_DATE", "2026-07-04"),
        int(os.environ.get("HEALTH_RECONCILE_DAYS", "7")),
    )

    user_creds = load_user_credentials()
    health = GoogleHealthClient(user_creds)
    # Fetch a padded window (local-day attribution can differ from UTC by a day);
    # fall back to an unbounded fetch if the API rejects the time filter.
    fetch_from = None
    if start:
        padded = date.fromisoformat(start) - timedelta(days=2)
        fetch_from = f"{padded.isoformat()}T00:00:00Z"
    try:
        weight_points = health.list_data_points(WEIGHT, start_time=fetch_from)
        fat_points = health.list_data_points(BODY_FAT, start_time=fetch_from)
    except requests.HTTPError as err:
        if fetch_from and err.response is not None and err.response.status_code == 400:
            weight_points = health.list_data_points(WEIGHT)
            fat_points = health.list_data_points(BODY_FAT)
        else:
            raise

    sa_creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    sheet = SheetClient(sa_creds, spreadsheet_id)
    sheet.ensure_tab(DAILY_TAB, DAILY_HEADERS)

    body = daily_body(weight_points, fat_points, start)
    meals = sheet.read_rows(MEALS_TAB)
    nutrition = daily_nutrition(meals, start)
    daily_result = sheet.upsert_daily(build_daily_rows(body, nutrition))

    dashboard_note = "refreshed"
    try:
        refresh_dashboard(sheet)
    except Exception as err:  # stats are cosmetic — never fail the data run
        dashboard_note = f"skipped ({err})"

    print(
        f"window>={start or 'ALL'}: fetched {len(weight_points)} weight / "
        f"{len(fat_points)} body-fat points; read {len(meals)} meal rows; "
        f"daily_summary updated {daily_result['updated']}, appended "
        f"{daily_result['appended']}; dashboard {dashboard_note} "
        f"(spreadsheet {spreadsheet_id}, project {project})."
    )


if __name__ == "__main__":
    main()
