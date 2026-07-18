"""Unit tests for the daily job's pure transforms (no network, no sheet).

The job is nutrition-only now: body composition arrives through the ingest
service (see tests/test_ingest.py), not through a scheduled pull.
"""
import json
from datetime import date, timedelta

from src.run_daily import (
    _is_true, build_daily_rows, daily_nutrition, fetch_biometrics, nutrition_day,
    window_start,
)
from src.sheets import DAILY_TAB


# -- what is final when (the wake-day vs civil-day split) -------------------------
class _FakeHealth:
    """Records the windows fetch_biometrics asks each family for."""

    def __init__(self):
        self.list_windows = {}
        self.rollup_windows = {}

    def list_data_points(self, data_type, *, family="daily", start=None, end=None):
        self.list_windows[data_type] = (start, end)
        return []

    def daily_rollup(self, data_type, start, end):
        self.rollup_windows[data_type] = (start.isoformat(), end.isoformat())
        return []


def test_activity_stops_at_today_while_sleep_runs_through_it():
    # The bug this encodes: one shared window put a HALF-DAY's calories on a day
    # still under way (903 kcal at 11:00 reads exactly like a finished day), while
    # sleep — which IS final once you wake — was fine to fetch for today.
    client = _FakeHealth()
    fetch_biometrics(client, "2026-07-10", "2026-07-18", activity_end="2026-07-17")

    # sleep + recovery run through tomorrow, so the night that just ended lands on
    # today's row
    assert client.list_windows["sleep"] == ("2026-07-10", "2026-07-18")
    assert client.list_windows["daily-resting-heart-rate"] == ("2026-07-10",
                                                               "2026-07-18")
    # activity stops at today (exclusive) — today's partial day is never asked for
    assert client.rollup_windows["total-calories"] == ("2026-07-10", "2026-07-17")
    assert client.rollup_windows["steps"] == ("2026-07-10", "2026-07-17")


def test_activity_end_defaults_to_end_for_plain_backfills():
    client = _FakeHealth()
    fetch_biometrics(client, "2026-07-10", "2026-07-17")
    assert client.rollup_windows["total-calories"] == ("2026-07-10", "2026-07-17")


def test_no_rollup_is_requested_when_no_civil_day_has_finished_yet():
    # start == activity_end would be an empty (or inverted) range; the API 400s on
    # some of those, and there is nothing to ask for anyway.
    client = _FakeHealth()
    fetch_biometrics(client, "2026-07-17", "2026-07-18", activity_end="2026-07-17")
    assert client.rollup_windows == {}
    assert client.list_windows["sleep"] == ("2026-07-17", "2026-07-18")

# -- nutrition rollup -------------------------------------------------------------
def test_daily_nutrition_sums_and_excludes_noise():
    meals = [
        {"datetime": "2026-07-10T14:34:46+01:00", "foods": "prosciutto",
         "calories": 78, "protein_g": 7.8, "carbs_g": 0, "fat_g": 5.7},
        {"datetime": "2026-07-10T15:06:31+01:00", "foods": "orange",
         "calories": 62, "protein_g": 1.2, "carbs_g": 15.4, "fat_g": 0.2},
        {"datetime": "2026-07-10T16:00:00+01:00", "foods": "not food",
         "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        {"datetime": "2026-07-10T17:00:00+01:00", "foods": "analysis failed",
         "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        {"datetime": "2026-07-10T18:00:00+01:00", "foods": "mystery zeros",
         "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    ]
    nut = daily_nutrition(meals, None)
    assert nut == {"2026-07-10": {
        "total_cals_in": 140.0, "total_protein_g": 9.0,
        "total_carbs_g": 15.4, "total_fat_g": 5.9,
    }}


def test_daily_nutrition_window_filter():
    meals = [{"datetime": "2026-07-01T12:00:00+01:00", "foods": "rice",
              "calories": 200, "protein_g": 4, "carbs_g": 44, "fat_g": 0.4}]
    assert daily_nutrition(meals, "2026-07-02") == {}


# -- waking-day (05:00 cutoff) attribution ------------------------------------
def test_nutrition_day_shifts_pre_cutoff_times_to_previous_day():
    assert nutrition_day("2026-07-14T00:17:00+01:00") == "2026-07-13"  # pre-bed dessert
    assert nutrition_day("2026-07-14T04:59:00+01:00") == "2026-07-13"  # still "night"
    assert nutrition_day("2026-07-14T05:00:00+01:00") == "2026-07-14"  # cutoff = new day
    assert nutrition_day("2026-07-14T13:00:00+01:00") == "2026-07-14"  # daytime
    assert nutrition_day("2026-07-14") == "2026-07-14"                 # date-only unshifted
    assert nutrition_day("") == ""


def test_daily_nutrition_counts_post_midnight_meal_on_previous_day():
    # the exact bug: a dessert at 00:17 must join yesterday's total, not start a
    # new day. Both meals land on 2026-07-13.
    meals = [
        {"datetime": "2026-07-13T20:00:00+01:00", "foods": "dinner",
         "calories": 600, "protein_g": 40, "carbs_g": 50, "fat_g": 20},
        {"datetime": "2026-07-14T00:17:00+01:00", "foods": "ice cream",
         "calories": 300, "protein_g": 10, "carbs_g": 30, "fat_g": 15},
    ]
    nut = daily_nutrition(meals, None, 5, in_progress_day="2026-07-20")
    assert set(nut) == {"2026-07-13"}
    assert nut["2026-07-13"]["total_cals_in"] == 900.0
    assert nut["2026-07-13"]["total_protein_g"] == 50.0


def test_daily_nutrition_excludes_the_in_progress_day():
    meals = [{"datetime": "2026-07-14T13:00:00+01:00", "foods": "lunch",
              "calories": 500, "protein_g": 30, "carbs_g": 40, "fat_g": 15}]
    # today (still under way) is not totalled…
    assert daily_nutrition(meals, None, 5, in_progress_day="2026-07-14") == {}
    # …but once the day is over it is
    assert "2026-07-14" in daily_nutrition(meals, None, 5, in_progress_day="2026-07-15")


def test_daily_nutrition_rolls_up_tier1_nutrients_from_items():
    meals = [
        {"datetime": "2026-07-12T08:00:00+01:00", "foods": "oats",
         "calories": 300, "protein_g": 10, "carbs_g": 50, "fat_g": 6,
         "items": json.dumps([{"name": "oats", "calories": 300, "protein_g": 10,
             "carbs_g": 50, "fat_g": 6,
             "nutrients": {"fiber_g": 8, "sodium_mg": 5, "iron_mg": 2}}])},
        {"datetime": "2026-07-12T19:00:00+01:00", "foods": "banana",
         "calories": 100, "protein_g": 1, "carbs_g": 27, "fat_g": 0,
         "items": json.dumps([{"name": "banana", "calories": 100, "protein_g": 1,
             "carbs_g": 27, "fat_g": 0,
             "nutrients": {"fiber_g": 3, "potassium_mg": 400}}])},
    ]
    nut = daily_nutrition(meals, None)["2026-07-12"]
    assert nut["total_cals_in"] == 400.0
    assert nut["total_fiber_g"] == 11.0        # 8 + 3
    assert nut["total_sodium_mg"] == 5.0
    assert nut["total_iron_mg"] == 2.0
    assert nut["total_potassium_mg"] == 400.0
    assert "total_calcium_mg" not in nut       # never present -> omitted, not 0


# -- merge rows -------------------------------------------------------------------
def test_build_daily_rows_carries_only_the_columns_it_owns():
    # The job owns biometrics + nutrition and nothing else. It must never emit a
    # body column, or `upsert_daily` would overwrite the scale reading the ingest
    # service wrote when the user stepped on the scale.
    rows = build_daily_rows({
        "2026-07-11": {"total_cals_in": 1800.0, "total_protein_g": 120.0},
        "2026-07-10": {"total_cals_in": 500.0},
    })
    assert rows == [                       # sorted by day
        {"date": "2026-07-10", "total_cals_in": 500.0},
        {"date": "2026-07-11", "total_cals_in": 1800.0, "total_protein_g": 120.0},
    ]
    assert not any(k.startswith("weight") or k.startswith("body") or "mass" in k
                   for row in rows for k in row)


def test_build_daily_rows_folds_sources_into_one_row_per_date():
    # The contract upsert_daily depends on: ONE row per date. Two rows for the same
    # date would be merged against the same grid snapshot, so the second would
    # clobber the first's columns — or append the day twice if it were new.
    rows = build_daily_rows(
        {"2026-07-16": {"steps": 728, "hrv_ms": 73.1}},      # biometrics
        {"2026-07-16": {"total_cals_in": 2000.0}},           # nutrition
    )
    assert rows == [{"date": "2026-07-16", "steps": 728, "hrv_ms": 73.1,
                     "total_cals_in": 2000.0}]


def test_build_daily_rows_unions_dates_across_sources():
    # a day with only a weigh-in-less Fitbit night, and a day with only meals
    rows = build_daily_rows({"2026-07-16": {"steps": 728}},
                            {"2026-07-15": {"total_cals_in": 2000.0}})
    assert [r["date"] for r in rows] == ["2026-07-15", "2026-07-16"]
    assert build_daily_rows() == []


# -- bowel-movement flag + dashboard tally ----------------------------------------
def test_is_true_reads_the_boolean_flag():
    assert _is_true(True)
    assert _is_true("TRUE") and _is_true("true")   # tolerate the string form
    for falsy in (False, "", None, "FALSE", 0):
        assert not _is_true(falsy)



# -- window -----------------------------------------------------------------------
def test_window_start_combines_bounds():
    today = date(2026, 7, 11)
    assert window_start("2026-07-04", 7, today) == "2026-07-04"
    assert window_start("2026-01-01", 7, today) == "2026-07-04"  # reconcile wins
    assert window_start("", 7, today) == "2026-07-04"
    assert window_start("2026-07-04", 0, today) == "2026-07-04"  # floor only
    assert window_start("", 0, today) is None  # unbounded backfill
