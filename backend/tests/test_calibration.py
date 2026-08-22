"""Tests for the personal calibration of the energy-balance model.

The property that actually matters is the one in test_recovers_a_known_bias: if we
inject a known device error into synthetic data, the estimator must find it. Most
of the rest guard the four rules in src/calibration.py's docstring — especially
rule 3 (estimate on the past, apply to the future), which is the one whose breakage
would be silent and would quietly make the whole module worthless.
"""
from src.calibration import (
    CALIBRATION_HEADERS, MIN_DAYS, PRIOR_SD_KCAL, RHO_KCAL_PER_KG,
    adjusted_balances, calibration_series, current, estimate,
)

COL = {name: i for i, name in enumerate(CALIBRATION_HEADERS)}

# A repeating, deterministic wobble standing in for water/gut noise. Real residual
# sd is ~0.31 kg (measured over 2026-07-17..08-18); this has a similar spread and
# sums to zero over its period, so it perturbs the fit without shifting the trend.
NOISE = [0.0, 0.35, -0.30, 0.15, -0.40, 0.25, -0.05]


def make_days(n, bias=0.0, balance=-700.0, start_kg=80.0, noisy=True):
    """`n` days where the TRUE energy balance is `balance + bias`.

    The sheet records `balance`; the body responds to `balance + bias`. So a
    positive `bias` means the log overstates the deficit — exactly the situation
    found in the real data (+250 kcal/day) — and the estimator should recover it.
    """
    true_daily_kg = (balance + bias) / RHO_KCAL_PER_KG
    days = []
    for i in range(n):
        wobble = NOISE[i % len(NOISE)] if noisy else 0.0
        days.append({
            "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "weight_kg": round(start_kg + true_daily_kg * i + wobble, 2),
            "energy_balance_kcal": balance,
        })
    return days


def test_recovers_a_known_bias():
    """The headline property. Inject +250 kcal/day of device error; find it."""
    cal = estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                    for d in make_days(120, bias=250.0)])
    assert cal is not None
    assert abs(cal.bias_raw_kcal - 250.0) < 25.0, cal.bias_raw_kcal
    lo, hi = cal.ci
    assert lo < 250.0 < hi, (lo, hi)


def test_no_bias_when_devices_are_honest():
    cal = estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                    for d in make_days(120, bias=0.0)])
    assert abs(cal.bias_raw_kcal) < 25.0, cal.bias_raw_kcal


def test_precision_improves_with_more_days():
    """se should fall steeply with window length — the N^-1.5 result that makes
    this whole approach worth building. Not asserting the exponent, just that a
    longer window is decisively better."""
    def se(n):
        return estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                         for d in make_days(n, bias=250.0)]).se_kcal
    assert se(120) < se(60) < se(30)
    assert se(120) < se(30) / 4


def test_shrinkage_damps_a_weak_estimate():
    """Rule 2. A short window must not fire its full raw correction."""
    short = estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                      for d in make_days(14, bias=250.0)])
    long = estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                     for d in make_days(120, bias=250.0)])
    assert short.shrink < long.shrink
    assert abs(short.bias_applied_kcal) < abs(short.bias_raw_kcal)
    assert long.shrink > 0.95
    # A perfect (noiseless) fit is maximum confidence, not zero confidence.
    clean = estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                      for d in make_days(60, bias=250.0, noisy=False)])
    assert clean.shrink > 0.99
    assert abs(clean.bias_applied_kcal - 250.0) < 5.0


def test_too_little_history_publishes_nothing():
    assert estimate([(d["date"], d["weight_kg"], d["energy_balance_kcal"])
                     for d in make_days(MIN_DAYS - 1)]) is None


def test_series_is_strictly_out_of_sample():
    """Rule 3, the one that must never regress.

    Every row's fitting window has to END BEFORE that row's own date. If this ever
    fails, the adjusted balance starts matching the scale by construction and the
    module silently stops being able to detect a device going wrong.
    """
    rows = calibration_series(make_days(60, bias=250.0))
    fitted = [r for r in rows if r[COL["window_end"]] != ""]
    assert fitted, "expected some fitted rows"
    for r in fitted:
        assert r[COL["window_end"]] < r[COL["date"]], r


def test_series_row_ignores_its_own_future():
    """Corollary of rule 3: mutating the LAST day cannot change earlier rows."""
    days = make_days(60, bias=250.0)
    before = calibration_series(days)
    days[-1]["weight_kg"] = 999.0  # absurd, but it is the future for every prior row
    after = calibration_series(days)
    assert before[:-1] == after[:-1]


def test_early_rows_are_marked_not_silently_zeroed():
    rows = calibration_series(make_days(60, bias=250.0))
    assert rows[0][COL["status"]] == "insufficient_history"
    assert rows[0][COL["bias_applied_kcal"]] == ""


def test_adjusted_balance_applies_the_correction():
    days = make_days(60, bias=250.0)
    adj = adjusted_balances(days)
    assert adj, "expected some adjusted days"
    series = {r[COL["date"]]: r for r in calibration_series(days)}
    for day, value in adj.items():
        raw = next(d["energy_balance_kcal"] for d in days if d["date"] == day)
        assert value == round(raw + series[day][COL["bias_applied_kcal"]])
        # The correction shrinks the recorded deficit toward reality.
        assert value > raw


def test_days_without_history_are_omitted_not_left_uncorrected():
    """A number that looks corrected but isn't is worse than a blank."""
    days = make_days(60, bias=250.0)
    adj = adjusted_balances(days)
    assert days[0]["date"] not in adj


def test_blank_and_ragged_rows_do_not_crash():
    days = make_days(40, bias=250.0)
    days[5]["weight_kg"] = ""          # missed weigh-in
    days[6].pop("energy_balance_kcal")  # day not yet totalled
    days[7]["energy_balance_kcal"] = None
    days[8]["weight_kg"] = "not a number"
    cal = current(days)
    assert cal is not None
    assert abs(cal.bias_raw_kcal - 250.0) < 60.0, cal.bias_raw_kcal


def test_drift_is_flagged_when_the_recent_window_disagrees():
    """Rule 4. A device that starts misreading must surface, not be absorbed."""
    steady = make_days(90, bias=250.0)
    # Something breaks: the last 30 days carry a much larger error.
    broken = make_days(30, bias=1400.0, start_kg=steady[-1]["weight_kg"])
    for i, d in enumerate(broken):
        d["date"] = f"2026-06-{1 + i:02d}"
    cal = current(steady + broken)
    assert cal.status == "drift", cal
    assert "DRIFT" in cal.note


def test_no_drift_flag_when_the_bias_is_stable():
    assert current(make_days(120, bias=250.0)).status == "ok"


def test_row_shape_matches_headers():
    for row in calibration_series(make_days(40, bias=250.0)):
        assert len(row) == len(CALIBRATION_HEADERS)
