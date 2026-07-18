"""Cloud entry point: refresh the per-day health model.

Each run (Cloud Run Job):

  1. Pulls **Fitbit Air biometrics** from the Google Health API — sleep (stages,
     efficiency, naps kept apart), overnight recovery (resting HR, HRV, SpO2,
     respiration, skin temperature) and activity (steps, distance, calories out,
     active/zone minutes, heart-rate range) — and merges them per civil day.
  2. Rolls meals logged in the ``meals`` tab into ``daily_summary`` nutrition
     totals per day; non-food shots and failed analyses are ignored.

**What triggers a run, and why it is not a clock.** The job is kicked by the
*weigh-in*: the scale screenshot the user sends on waking (ingest/main.py ->
_trigger_daily_sync). That screenshot is a semantic "I am awake" event, and it is
the only reliable one this system gets — which matters because everything here
keys off the night being over.

This replaced a 07:00 cron whose docstring claimed it ran "after the night has
been scored and synced". It did not: the user wakes at ~08:30, so 07:00 ran while
they were still ASLEEP. The night was not over, let alone scored — so every night's
sleep missed its own row and only appeared ~24 h later, when the next morning's run
re-rolled it inside HEALTH_RECONCILE_DAYS. Sleep was silently a day late, always.

A cron remains at 11:00 purely as a backstop for days with no weigh-in; by then the
night has ended and synced regardless. It is idempotent, so on a normal day it is a
no-op re-roll.

Each family becomes final at a different moment, which is the whole shape of this
file (see fetch_biometrics and daily_nutrition):

  * sleep + recovery -> final when the user WAKES  -> written to today's row
  * activity         -> final at MIDNIGHT          -> today is never written
  * nutrition        -> final at the 05:00 cutoff  -> today is never totalled

So a fresh row grows in three steps: this morning it holds last night's sleep,
recovery and the weigh-in; tonight's run adds nothing; tomorrow's weigh-in closes
it with activity and nutrition.

Body composition is **not** collected here. It arrives through the ingest service
the moment the user screenshots their smart-scale app (see ingest/main.py) and is
written straight into ``daily_summary``'s body columns, keyed on the reading's own
date. Each source fills only the columns it owns, so nothing clobbers anything.

Nutrition uses a **waking-day** grain: a meal counts toward the day that started
at ``NUTRITION_DAY_CUTOFF_HOUR`` (05:00 local by default), so a dessert eaten at
00:17 — before bed — rolls into *yesterday*, not the new calendar day. The
current, still-in-progress nutrition day is **not** totalled at all: a day's
intake is only summed once it is over (after 05:00 the next morning), so the sheet
never shows a misleading partial total for a day still under way. That waking-day
rule is precisely why this job is still on a timer at all — a finished day has to
be totalled *after* it finishes.

Only the trailing ``HEALTH_RECONCILE_DAYS`` days are re-rolled, so a late
correction to an old meal — or a night Fitbit re-scores after the fact — still
lands. Set it to 0 for a full re-roll from source.

Reads HEALTH_OAUTH_TOKEN (Secret Manager; **health scopes only** — the API 403s on
a token that also carries Drive) for the biometrics; writes the Sheet with the
runtime service account (ADC, Sheets scope).
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from src.analysis import (
    BASELINE_HEADERS,
    baseline_rows,
)
from src.biometrics import biometric_days, daily_activity, daily_recovery, daily_sleep
from src.google_health import (
    DAILY_TYPES, ROLLUP_TYPES, SLEEP, GoogleHealthClient,
)
from src.sheets import (
    BASELINES_TAB, DAILY_HEADERS, DAILY_TAB, MEALS_TAB, SheetClient, TIER1_NUTRIENTS,
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


def _is_true(value: Any) -> bool:
    """A daily boolean flag (e.g. bowel_movement) reads back as Python True from an
    UNFORMATTED_VALUE fetch, but tolerate the string form too."""
    return value is True or str(value).strip().upper() == "TRUE"


# -- Fitbit Air biometrics -----------------------------------------------------
def load_health_credentials() -> Credentials:
    """The health-only user token. Health data needs *user* consent, so a service
    account can't read it. This token must carry no Drive scope (see src/auth.py)."""
    raw = os.environ.get("HEALTH_OAUTH_TOKEN")
    if raw:
        info = json.loads(raw)
    else:
        from src.auth import token_file  # local fallback for manual runs

        info = json.loads(token_file("health").read_text())
    creds = Credentials.from_authorized_user_info(info)
    creds.refresh(Request())
    return creds


def fetch_biometrics(client: GoogleHealthClient, start: str, end: str,
                     activity_end: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Every biometric column for [start, end), as date -> columns.

    Sleep and the daily-* summaries come from `list`; movement/energy come from
    `dailyRollUp`, which aggregates server-side over civil days (and is the only
    route to `total-calories`). A data type the tracker never produced just comes
    back empty and its columns stay blank.

    `activity_end` (exclusive; defaults to `end`) bounds the ROLLUP types on their
    own, because the three families stop changing at different moments and lumping
    them together is what put a half-day's calories on a day still under way:

      * **sleep** and **recovery** describe the night that just ENDED. They are
        final the moment the user wakes, so today is fair game — indeed today's row
        is exactly where they belong (sleep is keyed on the wake day).
      * **activity** accumulates until midnight. Today's rollup is a *partial* day
        that looks identical to a finished one — 903 kcal at 11:00 reads like a
        person who burned 903 kcal all day. Callers pass today here so it is never
        fetched, matching the rule nutrition already follows: only total a day once
        it is over.
    """
    sleep_points = client.list_data_points(SLEEP, family="sleep", start=start, end=end)
    recovery_points = {
        data_type: client.list_data_points(data_type, family="daily",
                                           start=start, end=end)
        for data_type in DAILY_TYPES
    }
    first = date.fromisoformat(start)
    last_activity = date.fromisoformat(activity_end or end)
    activity_points = {
        data_type: client.daily_rollup(data_type, first, last_activity)
        for data_type in ROLLUP_TYPES
    } if last_activity > first else {}
    return biometric_days(
        daily_sleep(sleep_points),
        daily_recovery(recovery_points),
        daily_activity(activity_points),
        start=start,
    )


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


def _with_energy_balance(row: Dict[str, Any]) -> Dict[str, Any]:
    """Derive `energy_balance_kcal` = intake - expenditure when both sides exist.

    Stored rather than left to the reader because it is the headline number of the
    whole system, and because the sheet is meant to stand alone for AI analysis.
    Positive = surplus. Blank when either side is missing — a one-sided balance is
    not a small balance, it's an unknown one."""
    cals_in, cals_out = _num(row.get("total_cals_in")), _num(row.get("total_cals_out"))
    if row.get("total_cals_in") not in (None, "") and \
            row.get("total_cals_out") not in (None, ""):
        row["energy_balance_kcal"] = round(cals_in - cals_out)
    return row


def build_daily_rows(*sources: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold independent per-day column groups (biometrics, nutrition) into ONE
    merge-row per date.

    Exactly one row per date is the contract `upsert_daily` relies on: it merges
    every row against the same pre-read grid, so two rows for one date would make
    the second overwrite the first's columns with stale values (or append the day
    twice if it's new). Columns absent here are left to their owners — the scale
    screenshot and /feel write their own.
    """
    days: set = set()
    for source in sources:
        days |= set(source)
    rows: List[Dict[str, Any]] = []
    for day in sorted(days):
        row: Dict[str, Any] = {"date": day}
        for source in sources:
            row.update(source.get(day, {}))
        rows.append(_with_energy_balance(row))
    return rows


# -- derived views --------------------------------------------------------------
def rebuild_views(sheet: SheetClient) -> Dict[str, int]:
    """Regenerate the `analysis` and `baselines` tabs from daily_summary.

    Wholesale replacement, not incremental: these are pure functions of the
    observations, so rebuilding is both simpler and drift-proof. A stale derived
    row is worse than no row — it looks like data.
    """
    rows = sorted(sheet.read_rows(DAILY_TAB), key=lambda r: str(r.get("date", "")))

    baselines = baseline_rows(rows)
    sheet.replace_tab(BASELINES_TAB, [BASELINE_HEADERS] + baselines)
    return {"baselines": len(baselines)}



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

    tz = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))
    today = datetime.now(tz).date()

    # 1. Fitbit Air biometrics. Never fatal: a token/API problem must not also cost
    #    us the nutrition roll-up, which needs nothing but the Sheet.
    biometrics: Dict[str, Dict[str, Any]] = {}
    bio_note = "skipped (no window)"
    if start:
        try:
            client = GoogleHealthClient(load_health_credentials())
            # Two different ends, on purpose (see fetch_biometrics): sleep and
            # recovery run to tomorrow so the night that just ended lands on TODAY's
            # row; activity stops at today so a still-running day is never rolled up
            # as if it were finished.
            biometrics = fetch_biometrics(
                client, start,
                (today + timedelta(days=1)).isoformat(),
                activity_end=today.isoformat(),
            )
            bio_note = f"{len(biometrics)} day(s)"
        except Exception as err:
            bio_note = f"FAILED ({err})"
            print(f"biometrics: {bio_note}")

    # 2. Nutrition. The day still under way (local now shifted back by the cutoff)
    #    is excluded, so only completed days are totalled.
    meals = sheet.read_rows(MEALS_TAB)
    in_progress = (datetime.now(tz) - timedelta(hours=DAY_CUTOFF_HOUR)).date().isoformat()
    nutrition = daily_nutrition(meals, start, DAY_CUTOFF_HOUR, in_progress)

    daily_result = sheet.upsert_daily(build_daily_rows(biometrics, nutrition))

    # Unconditional, so the tab self-heals: rows arrive in submission order, and a
    # backfilled scale screenshot appends a day that belongs above the ones already
    # there. Cheap (one call/day) and idempotent when already sorted.
    sort_note = "sorted"
    try:
        sheet.sort_by_date(DAILY_TAB)
    except Exception as err:  # ordering is cosmetic — never fail the data run
        sort_note = f"sort skipped ({err})"

    # Derived views, rebuilt wholesale from the observations we just wrote. All
    # three are cosmetic in the sense that the source of truth is daily_summary —
    # so none of them may fail the data run.
    views_note = "rebuilt"
    try:
        rebuild_views(sheet)
    except Exception as err:
        views_note = f"skipped ({err})"

    print(
        f"window>={start or 'ALL'}: biometrics {bio_note}; read {len(meals)} "
        f"meal rows -> {len(nutrition)} nutrition day(s); daily_summary updated "
        f"{daily_result['updated']}, appended {daily_result['appended']}, "
        f"{sort_note}; views {views_note} "
        f"(spreadsheet {spreadsheet_id}, project {project})."
    )


if __name__ == "__main__":
    main()
