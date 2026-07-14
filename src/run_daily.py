"""Cloud entry point: roll the day's meals up into the per-day health model.

Each run (Cloud Run Job, triggered ~07:00 Europe/Lisbon):

  1. Rolls meals logged in the ``meals`` tab into ``daily_summary`` nutrition
     totals per day; non-food shots and failed analyses are ignored.
  2. Refreshes the ``dashboard`` tab's stat cells (best-effort).

Body composition is **not** collected here. It arrives through the ingest service
the moment the user screenshots their smart-scale app (see ingest/main.py) and is
written straight into ``daily_summary``'s body columns, keyed on the reading's own
date. This job's merge-upsert only ever fills the nutrition columns, so those
readings are never clobbered.

Nutrition uses a **waking-day** grain: a meal counts toward the day that started
at ``NUTRITION_DAY_CUTOFF_HOUR`` (05:00 local by default), so a dessert eaten at
00:17 — before bed — rolls into *yesterday*, not the new calendar day. The
current, still-in-progress nutrition day is **not** totalled at all: a day's
intake is only summed once it is over (after 05:00 the next morning), so the sheet
never shows a misleading partial total for a day still under way. That waking-day
rule is precisely why this job is still on a timer at all — a finished day has to
be totalled *after* it finishes.

Only the trailing ``HEALTH_RECONCILE_DAYS`` days are re-rolled, so a late
correction to an old meal still lands. Set it to 0 for a full re-roll from source.

Readiness columns (sleep/HRV/SpO2/skin-temp/steps/active-mins) are left blank
here; the Fitbit biometrics step fills them through the same merge-upsert.

Writes the Sheet with the runtime service account (ADC, Sheets scope). No user
OAuth token, no health scopes — this job talks to nothing but the Sheet.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import google.auth

from src.sheets import (
    DAILY_HEADERS, DAILY_TAB, DASHBOARD_FIRST_ROW, DASHBOARD_STATS, DASHBOARD_TAB,
    MEALS_TAB, SheetClient, TIER1_NUTRIENTS,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Meal rows excluded from nutrition totals (kept in sync with ingest/main.py).
NON_MEALS = {"not food", "analysis failed"}

# The "nutrition day" starts at this local hour: a meal before it counts toward
# the previous day (a 00:17 pre-bed dessert belongs to yesterday, not the new
# calendar day). 05:00 is a safe waking cutoff — earlier than any breakfast, past
# any late-night snack. Tune via NUTRITION_DAY_CUTOFF_HOUR.
DAY_CUTOFF_HOUR = int(os.environ.get("NUTRITION_DAY_CUTOFF_HOUR", "5"))


def nutrition_day(dt_str: Any, cutoff_hour: int = DAY_CUTOFF_HOUR) -> str:
    """The waking-day (YYYY-MM-DD) a meal's local datetime belongs to: the local
    calendar day shifted back by `cutoff_hour`, so times before the cutoff fall on
    the previous day. Date-only/empty/unparseable inputs pass through unshifted."""
    s = str(dt_str or "")
    if "T" not in s:              # date-only or empty — nothing to shift
        return s[:10]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s[:10]
    return (dt - timedelta(hours=cutoff_hour)).date().isoformat()


def window_start(start_date: str, reconcile_days: int,
                 today: Optional[Any] = None) -> Optional[str]:
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


# -- nutrition roll-up ---------------------------------------------------------
def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    """The meals `items` cell holds a JSON array of per-ingredient objects."""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def daily_nutrition(meals: List[Dict[str, Any]], start: Optional[str],
                    cutoff_hour: int = DAY_CUTOFF_HOUR,
                    in_progress_day: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Per **waking day** (05:00-anchored, see `nutrition_day`): sum macros (flat
    meal columns) and Tier-1 micronutrients (from each meal's per-ingredient
    `items` JSON). Non-food and zero-content rows are ignored. Nutrient totals are
    emitted only when non-zero, so days before nutrient tracking stay blank rather
    than showing misleading zeros.

    `in_progress_day`, when given, is the current (still-running) nutrition day;
    it and anything after are excluded, so a day's intake is only totalled once it
    is over — the sheet never shows a half-finished day's running sum."""
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
        day = nutrition_day(meal.get("datetime"), cutoff_hour)
        if not day or not _in_window(day, start):
            continue
        if in_progress_day is not None and day >= in_progress_day:
            continue  # this nutrition day isn't over yet — don't total it
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


def build_daily_rows(nutrition: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One merge-row per day. Nutrition columns only — every other column belongs
    to another source and `upsert_daily` leaves the ones we omit untouched."""
    return [{"date": day, **nutrition[day]} for day in sorted(nutrition)]


# -- dashboard -----------------------------------------------------------------
def refresh_dashboard(sheet: SheetClient) -> None:
    """Rewrite the dashboard's stat column from DASHBOARD_STATS (maintenance.py
    writes the matching labels beside them). No-op if the tab is absent."""
    if DASHBOARD_TAB not in sheet.tab_titles():
        return
    rows = sorted(sheet.read_rows(DAILY_TAB), key=lambda r: str(r.get("date", "")))
    logged = [r for r in rows if _num(r.get("total_cals_in")) > 0][-7:]

    def latest(col: str) -> Any:
        """The most recent non-empty reading — body columns are only filled on days
        the user actually weighed in, so the last *row* is usually blank."""
        for row in reversed(rows):
            value = row.get(col)
            if value not in (None, ""):
                return value
        return ""

    def avg7(col: str) -> Any:
        if not logged:
            return ""
        return round(sum(_num(r.get(col)) for r in logged) / len(logged))

    reduce = {
        "latest": latest,
        "avg7": avg7,
        "days7": lambda _col: len(logged),
        "now": lambda _col: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    stats = [[reduce[kind](col)] for _label, col, kind in DASHBOARD_STATS]
    sheet.write_values(DASHBOARD_TAB, f"B{DASHBOARD_FIRST_ROW}", stats)


# -- entry point ----------------------------------------------------------------
def main() -> None:
    spreadsheet_id = os.environ["HEALTH_SPREADSHEET_ID"]
    start = window_start(
        os.environ.get("HEALTH_START_DATE", "2026-07-04"),
        int(os.environ.get("HEALTH_RECONCILE_DAYS", "7")),
    )

    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    sheet = SheetClient(creds, spreadsheet_id)

    # Fail loudly rather than write through a stale schema: `upsert_daily` places
    # values by DAILY_HEADERS *position*, so a sheet that's missing a column would
    # silently shift every later value into the wrong column. src/maintenance.py is
    # the only thing allowed to change the physical layout (it inserts columns in
    # place, keeping history aligned).
    header = sheet.header(DAILY_TAB)
    if header != DAILY_HEADERS:
        missing = [h for h in DAILY_HEADERS if h not in header]
        raise RuntimeError(
            f"{DAILY_TAB} schema is stale (missing {missing or 'nothing, but order differs'}) "
            "— run `python -m src.maintenance` to migrate it, then re-run this job."
        )

    meals = sheet.read_rows(MEALS_TAB)
    # The nutrition day still under way (local now shifted back by the cutoff) is
    # excluded, so only completed days are totalled.
    tz = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))
    in_progress = (datetime.now(tz) - timedelta(hours=DAY_CUTOFF_HOUR)).date().isoformat()
    nutrition = daily_nutrition(meals, start, DAY_CUTOFF_HOUR, in_progress)
    daily_result = sheet.upsert_daily(build_daily_rows(nutrition))

    # Unconditional, so the tab self-heals: rows arrive in submission order, and a
    # backfilled scale screenshot appends a day that belongs above the ones already
    # there. Cheap (one call/day) and idempotent when already sorted.
    sort_note = "sorted"
    try:
        sheet.sort_by_date(DAILY_TAB)
    except Exception as err:  # ordering is cosmetic — never fail the data run
        sort_note = f"sort skipped ({err})"

    dashboard_note = "refreshed"
    try:
        refresh_dashboard(sheet)
    except Exception as err:  # stats are cosmetic — never fail the data run
        dashboard_note = f"skipped ({err})"

    print(
        f"window>={start or 'ALL'}: read {len(meals)} meal rows -> "
        f"{len(nutrition)} nutrition day(s); daily_summary updated "
        f"{daily_result['updated']}, appended {daily_result['appended']}, "
        f"{sort_note}; dashboard {dashboard_note} "
        f"(spreadsheet {spreadsheet_id}, project {project})."
    )


if __name__ == "__main__":
    main()
