"""Unit tests for sheet schema constants and helpers."""
from src.sheets import DAILY_HEADERS, TIER1_NUTRIENTS, col_letter


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


def test_tier1_nutrients_have_daily_columns():
    assert len(TIER1_NUTRIENTS) == 15
    for n in TIER1_NUTRIENTS:
        assert f"total_{n}" in DAILY_HEADERS
    # nutrient totals sit within the nutrition block, before activity/body
    i = DAILY_HEADERS.index
    assert i("total_fat_g") < i("total_fiber_g") < i("total_active_mins")
