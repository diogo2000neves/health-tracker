"""Unit tests for sheet schema constants and helpers."""
from src.sheets import (
    BODY_METRICS, DAILY_HEADERS, DASHBOARD_FIRST_ROW, DASHBOARD_STATS,
    TIER1_NUTRIENTS, col_letter,
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
    # a TRUE/blank marker that sits next to subjective_feel — both are things the
    # user self-reports about a day, not sensor data.
    assert "bowel_movement" in DAILY_HEADERS
    i = DAILY_HEADERS.index
    assert i("bowel_movement") == i("subjective_feel") + 1
    assert DAILY_HEADERS[-1] == "updated_at"  # still last


def test_tier1_nutrients_have_daily_columns():
    assert len(TIER1_NUTRIENTS) == 15
    for n in TIER1_NUTRIENTS:
        assert f"total_{n}" in DAILY_HEADERS
    # nutrient totals sit within the nutrition block, before activity/body
    i = DAILY_HEADERS.index
    assert i("total_fat_g") < i("total_fiber_g") < i("total_active_mins")


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


def test_dashboard_stats_reference_real_columns():
    # maintenance.py writes the labels and run_daily.py writes the values beside
    # them from this one list; a column that doesn't exist would render blank
    # forever, and a mismatched length would slide every number up a row.
    for _label, col, kind in DASHBOARD_STATS:
        assert kind in {"latest", "avg7", "count7", "days7", "now"}
        if kind in {"latest", "avg7", "count7"}:
            assert col in DAILY_HEADERS, col
        else:
            assert col == ""
    # the bowel-movement tally is surfaced (the point of the feature)
    assert ("Bowel movements (last 7 days)", "bowel_movement", "count7") in DASHBOARD_STATS
    assert DASHBOARD_FIRST_ROW == 3
    # every metric the scale gives us is surfaced, not just weight
    shown = {col for _label, col, _kind in DASHBOARD_STATS}
    for metric in BODY_METRICS:
        assert metric in shown
