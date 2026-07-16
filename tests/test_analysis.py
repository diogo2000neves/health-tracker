"""Tests for the `baselines` tab: what is normal for this person."""
from datetime import date

from src.analysis import BASELINE_HEADERS, baseline_rows

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
