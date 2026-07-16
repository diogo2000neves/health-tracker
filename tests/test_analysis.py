"""Tests for the derived views: causal alignment and personal baselines."""
from datetime import date

from src.analysis import (
    BASELINE_HEADERS, analysis_headers, analysis_rows, baseline_rows,
)

# Two real-shaped days: intake on the 15th, and the night/weigh-in that followed
# it recorded on the 16th.
DAILY = [
    {"date": "2026-07-15", "total_cals_in": 2000.0, "total_protein_g": 120.0,
     "steps": 4151, "total_cals_out": 2221, "sleep_mins": 470,
     "sleep_efficiency_pct": 96.5, "hrv_ms": 75.2, "resting_hr_bpm": 63,
     "weight_kg": 70.0},
    {"date": "2026-07-16", "total_cals_in": 1800.0, "total_protein_g": 100.0,
     "steps": 866, "total_cals_out": 1016, "sleep_mins": 525,
     "sleep_efficiency_pct": 98.3, "hrv_ms": 73.1, "resting_hr_bpm": 62,
     "weight_kg": 69.05},
]


def _row(rows, day):
    idx = {n: i for i, n in enumerate(analysis_headers())}
    for r in rows:
        if r[0] == day:
            return {n: r[i] for n, i in idx.items()}
    raise AssertionError(f"no analysis row for {day}")


# -- the causal fix ------------------------------------------------------------
def test_intake_is_paired_with_the_night_that_followed_it():
    # THE point of this module. On the raw table, the 15th's food sits beside the
    # 15th's sleep — which happened the night BEFORE that food was eaten. Here the
    # 15th's intake is paired with the 16th's sleep, which it could actually cause.
    r = _row(analysis_rows(DAILY), "2026-07-15")
    assert r["total_cals_in"] == 2000.0            # eaten on the 15th
    assert r["sleep_mins_next"] == 525             # the night of 15->16
    assert r["sleep_efficiency_pct_next"] == 98.3
    assert r["hrv_ms_next"] == 73.1
    assert r["weight_kg_next"] == 69.05            # the 16th's fasted weigh-in


def test_outcomes_are_never_taken_from_the_same_row():
    r = _row(analysis_rows(DAILY), "2026-07-15")
    # 470 / 96.5 / 75.2 / 70.0 are the 15th's OWN outcome values — they belong to
    # the 14th's inputs, and must not appear on the 15th's analysis row.
    assert r["sleep_mins_next"] != 470
    assert r["hrv_ms_next"] != 75.2
    assert r["weight_kg_next"] != 70.0


def test_a_gap_in_the_data_never_mispairs_days():
    # Monday's food must not be paired with Friday's sleep just because Friday is
    # the next ROW. Alignment is by real date, so a missing successor blanks out.
    sparse = [DAILY[0], {**DAILY[1], "date": "2026-07-20"}]
    r = _row(analysis_rows(sparse), "2026-07-15")
    assert r["total_cals_in"] == 2000.0
    assert r["sleep_mins_next"] == ""              # 16th absent -> blank, not the 20th


def test_days_with_no_inputs_are_skipped():
    # A row holding only outcomes would add a line whose values already appear as
    # `_next` on the day before.
    rows = analysis_rows([{"date": "2026-07-15", "sleep_mins": 470}])
    assert rows == []


def test_headers_split_inputs_from_next_day_outcomes():
    h = analysis_headers()
    assert h[0] == "date"
    assert "total_cals_in" in h and "steps" in h          # inputs, unsuffixed
    assert "sleep_mins_next" in h and "weight_kg_next" in h
    assert "sleep_mins" not in h                          # outcomes only as _next
    assert len(h) == len(set(h))


def test_analysis_is_sorted_and_complete():
    rows = analysis_rows(DAILY)
    assert [r[0] for r in rows] == ["2026-07-15", "2026-07-16"]
    assert all(len(r) == len(analysis_headers()) for r in rows)


# -- baselines -----------------------------------------------------------------
def _baseline(rows, metric):
    idx = {n: i for i, n in enumerate(BASELINE_HEADERS)}
    for r in rows:
        if r[0] == metric:
            return {n: r[i] for n, i in idx.items()}
    raise AssertionError(f"no baseline for {metric}")


def test_baselines_describe_what_is_normal_for_this_person():
    rows = baseline_rows(DAILY, today=date(2026, 7, 16))
    hrv = _baseline(rows, "hrv_ms")
    assert hrv["n"] == 2
    assert hrv["mean"] == 74.15
    assert hrv["min"] == 73.1 and hrv["max"] == 75.2
    assert hrv["latest"] == 73.1
    assert hrv["unit"] == "ms" and hrv["direction"] == "up_good"


def test_z_scores_need_enough_history_to_mean_anything():
    # With 2 readings an SD is noise, so no z-score is claimed.
    rows = baseline_rows(DAILY, today=date(2026, 7, 16))
    assert _baseline(rows, "hrv_ms")["latest_z"] == ""
    assert _baseline(rows, "hrv_ms")["interpretation"] == "no baseline yet"

    many = [{"date": f"2026-07-{d:02d}", "hrv_ms": v}
            for d, v in zip(range(10, 17), [70, 71, 69, 70, 72, 70, 85])]
    hrv = _baseline(baseline_rows(many, today=date(2026, 7, 16)), "hrv_ms")
    assert hrv["n"] == 7
    assert hrv["latest"] == 85
    assert hrv["latest_z"] > 2                      # a clear outlier
    assert "unusually high" in hrv["interpretation"]
    assert "good" in hrv["interpretation"]          # up_good -> high is good


def test_interpretation_knows_which_direction_is_bad():
    # Same statistical outlier, opposite meaning: a resting-HR spike is bad news.
    many = [{"date": f"2026-07-{d:02d}", "resting_hr_bpm": v}
            for d, v in zip(range(10, 17), [60, 61, 59, 60, 61, 60, 75])]
    row = _baseline(baseline_rows(many, today=date(2026, 7, 16)), "resting_hr_bpm")
    assert row["latest_z"] > 2
    assert "worse than usual" in row["interpretation"]


def test_baselines_only_look_at_the_recent_window():
    stale = [{"date": "2026-01-01", "hrv_ms": 200.0}] + DAILY
    rows = baseline_rows(stale, today=date(2026, 7, 16))
    assert _baseline(rows, "hrv_ms")["max"] == 75.2      # January is out of window


def test_metrics_with_no_readings_are_omitted():
    rows = baseline_rows(DAILY, today=date(2026, 7, 16))
    metrics = {r[0] for r in rows}
    assert "hrv_ms" in metrics
    assert "swim_strokes" not in metrics                 # never recorded here
