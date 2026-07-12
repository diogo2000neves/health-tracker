"""Unit tests for the daily job's pure transforms (no network, no sheet)."""
import json
from datetime import date

from src.run_daily import (
    _local_date, build_daily_rows, daily_body, daily_nutrition, window_start,
)


def _metric(physical, offset="0s", civil=None, **values):
    sample = {"physicalTime": physical, "utcOffset": offset}
    if civil:
        sample["civilTime"] = {"date": civil}
    return {"sampleTime": sample, **values}


def _weight_point(physical, grams, offset="0s", civil=None):
    return {"weight": _metric(physical, offset, civil, weightGrams=grams)}


def _fat_point(physical, pct, offset="0s", civil=None):
    return {"bodyFat": _metric(physical, offset, civil, percentage=pct)}


# -- local civil-day attribution ----------------------------------------------
def test_local_date_prefers_civil_time():
    metric = _metric("2026-07-03T23:30:00Z", "3600s",
                     civil={"year": 2026, "month": 7, "day": 4})
    assert _local_date(metric) == "2026-07-04"


def test_local_date_offset_rolls_past_midnight():
    # 23:30 UTC + 1h offset = 00:30 next local day (the bug the fix targets).
    metric = _metric("2026-07-03T23:30:00Z", "3600s")
    assert _local_date(metric) == "2026-07-04"


def test_local_date_zero_offset_stays_on_utc_day():
    assert _local_date(_metric("2025-12-27T11:24:13Z", "0s")) == "2025-12-27"


def test_local_date_fractional_seconds():
    assert _local_date(_metric("2026-07-03T21:45:02.905829Z", "3600s")) == "2026-07-03"


# -- body rollup ----------------------------------------------------------------
def test_daily_body_picks_earliest_reading_of_local_day():
    points = [
        _weight_point("2026-07-03T21:54:20Z", 69550, "3600s"),
        _weight_point("2026-07-03T18:00:46Z", 69250, "3600s"),  # earliest
        _weight_point("2026-07-03T21:53:16Z", 69550, "3600s"),
    ]
    body = daily_body(points, [], None)
    assert body == {"2026-07-03": {"weight_kg": 69.25, "body_fat_pct": None}}


def test_daily_body_respects_window():
    points = [
        _weight_point("2026-07-03T18:00:46Z", 69250, "3600s"),
        _weight_point("2025-12-27T11:24:13Z", 70000, "0s"),
    ]
    body = daily_body(points, [_fat_point("2026-07-03T18:00:46Z", 20.2, "3600s")],
                      "2026-01-01")
    assert list(body) == ["2026-07-03"]
    assert body["2026-07-03"] == {"weight_kg": 69.25, "body_fat_pct": 20.2}


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


# -- merge + lean mass ----------------------------------------------------------
def test_build_daily_rows_derives_lean_mass():
    rows = build_daily_rows(
        {"2026-07-10": {"weight_kg": 70.0, "body_fat_pct": 20.0}},
        {"2026-07-10": {"total_cals_in": 500.0}},
    )
    assert rows == [{
        "date": "2026-07-10", "weight_kg": 70.0, "body_fat_pct": 20.0,
        "total_cals_in": 500.0, "lean_mass_kg": 56.0,
    }]


def test_build_daily_rows_no_lean_without_fat():
    rows = build_daily_rows({"2026-07-10": {"weight_kg": 70.0, "body_fat_pct": None}}, {})
    assert "lean_mass_kg" not in rows[0]


# -- window -----------------------------------------------------------------------
def test_window_start_combines_bounds():
    today = date(2026, 7, 11)
    assert window_start("2026-07-04", 7, today) == "2026-07-04"
    assert window_start("2026-01-01", 7, today) == "2026-07-04"  # reconcile wins
    assert window_start("", 7, today) == "2026-07-04"
    assert window_start("2026-07-04", 0, today) == "2026-07-04"  # floor only
    assert window_start("", 0, today) is None  # unbounded backfill
