"""Unit tests for sheet schema constants and helpers."""
from src.biometrics import BIOMETRIC_COLUMNS
from src.sheets import (
    BODY_METRICS, DAILY_HEADERS, READ_LAST_COL, TIER1_NUTRIENTS, col_letter,
)


def test_col_letter():
    assert col_letter(0) == "A"
    assert col_letter(25) == "Z"
    assert col_letter(26) == "AA"
    assert col_letter(51) == "AZ"


def test_daily_schema_shape():
    # `date` keys the merge-upsert; bookkeeping stays last; lean mass sits with
    # the physique block it derives from.
    assert DAILY_HEADERS[0] == "date"
    assert DAILY_HEADERS[-1] == "updated_at"
    i = DAILY_HEADERS.index
    assert i("weight_kg") < i("body_fat_pct") < i("lean_mass_kg") < i("updated_at")
    assert len(DAILY_HEADERS) == len(set(DAILY_HEADERS))  # no duplicates


def test_bowel_movement_is_a_self_reported_daily_flag():
    # a TRUE/blank marker that is self-reported about a day, not sensor data.
    assert "bowel_movement" in DAILY_HEADERS
    i = DAILY_HEADERS.index
    assert i("bowel_movement") == i("date") + 1
    assert DAILY_HEADERS[-1] == "updated_at"  # still last


def test_every_biometric_column_is_in_the_schema():
    for col in BIOMETRIC_COLUMNS:
        assert col in DAILY_HEADERS, col
    # one contiguous block, so maintenance can insert into it cleanly
    positions = [DAILY_HEADERS.index(c) for c in BIOMETRIC_COLUMNS]
    assert positions == list(range(positions[0], positions[0] + len(BIOMETRIC_COLUMNS)))


def test_there_is_no_sleep_score_column():
    # Fitbit's 0-100 score is proprietary and appears nowhere in the Google Health
    # API (verified field-by-field). A column for it could only ever stay blank —
    # sleep_efficiency_pct is the honest, derivable stand-in.
    assert "sleep_score" not in DAILY_HEADERS
    assert "sleep_efficiency_pct" in DAILY_HEADERS


def test_read_range_covers_the_whole_schema_with_headroom():
    # daily_summary silently outgrew A:Z once; a short read truncates the header so
    # columns past the cut look "missing" and their writes land nowhere.
    def index(letters):
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return n - 1
    assert index(READ_LAST_COL) >= len(DAILY_HEADERS) - 1 + 20


def test_tier1_nutrients_have_daily_columns():
    assert len(TIER1_NUTRIENTS) == 15
    for n in TIER1_NUTRIENTS:
        assert f"total_{n}" in DAILY_HEADERS
    # nutrient totals sit inside the nutrition block: after the macros they extend,
    # and before the body-composition block
    i = DAILY_HEADERS.index
    assert i("total_fat_g") < i("total_fiber_g") < i("weight_kg")


def test_every_scale_metric_has_a_column():
    # The ten metrics the scale computes from bioimpedance. The Google Health API
    # only ever exposed the first three — Fitbit strips the rest on the way through
    # — which is the whole reason the screenshot replaced it.
    assert len(BODY_METRICS) == 10
    for metric in BODY_METRICS:
        assert metric in DAILY_HEADERS
    # one contiguous block, so maintenance.py inserts new columns into it cleanly
    positions = [DAILY_HEADERS.index(m) for m in BODY_METRICS]
    assert positions == list(range(positions[0], positions[0] + len(BODY_METRICS)))
    # the derived and stamped columns close the block
    i = DAILY_HEADERS.index
    assert i("metabolic_age") < i("lean_mass_kg") < i("body_measured_at")


