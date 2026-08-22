"""Personal calibration of the energy-balance model: measuring how wrong the
devices are, and correcting for it.

Rebuilt from scratch on every daily run, like `baselines`, so it can never drift
out of step with the observations it derives from.

===============================================================================
WHY THIS EXISTS  (read this before changing anything here)
===============================================================================

`energy_balance_kcal` is the difference of two *estimates*, neither of which is
measured:

  * `total_cals_out` — Fitbit's guess at expenditure, extrapolated from heart rate
    and steps.
  * `total_cals_in`  — a vision model's guess at what was on the plate.

Both carry error, and the errors do not cancel. Measured over 2026-07-17..08-18
(33 contiguous days, the first month of real data):

    recorded mean energy balance      -762 kcal/day
    cumulative over 32 days        -24,373 kcal   -> predicts -3.17 kg
    actual weight change (trend)                     -2.12 kg  95% CI [-2.48, -1.77]
    ------------------------------------------------------------------
    DISCREPANCY                     +250 kcal/day  95% CI [+165, +336]

The calorie prediction fell OUTSIDE the confidence interval of the weight trend,
so this is a real bias, not noise. Roughly a third of the deficit the sheet
reports is not happening in the body.

This module estimates that bias continuously and publishes a corrected figure.

===============================================================================
THE ESTIMATOR, AND WHY IT IS NOT (first_weight - last_weight)
===============================================================================

Endpoint differencing gets -2.70 kg; the OLS trend over all 33 rows gets -2.12 kg.
Both are arithmetically correct. The trend is the right *estimator* because:

  * A single weigh-in carries +/-0.61 kg (95%) of water/gut noise — residual sd is
    0.309 kg. Endpoint differencing bets the entire calibration on 2 of 33 rows.
  * Both endpoints in this dataset are atypical: the first three rows are all
    exactly 68.55 (one reading that looks carried-forward) and the last row is a
    low outlier. Those two artifacts alone move the endpoint answer by ~0.6 kg,
    which is ~140 kcal/day of pure artifact. There is deliberately NO
    stale-reading filter here — identical consecutive weights are legitimate on a
    0.05 kg-quantised scale, so dropping them would cost more than it saves. The
    trend estimator already dilutes them; that is the point of using it.

So: **fit the trend on weight LEVELS. Never difference.** Differencing inflates
the noise by exactly sqrt(2) (0.309 -> 0.413 kg, measured), throwing away
information for nothing.

===============================================================================
WHY THIS CONVERGES FAST  (the result that makes the whole idea worth building)
===============================================================================

    se(bias) = se(slope) * (N-1) * RHO / n_days   ~   sd * RHO * sqrt(12) / N^1.5

Precision improves as **N^-1.5**, not the usual N^-0.5. The intuition: the calorie
deficit *accumulates* every day (32 x 762 kcal is a large number) while the scale's
noise *does not* — weight is a level, not an increment, so the same +/-0.6 kg wobble
applies whether the window is one week or one year. Signal grows, noise stays put.

    N =  14 d   se ~ +/-157 kcal/day
    N =  32 d   se ~ +/- 46      (where we were on 2026-08-18)
    N =  60 d   se ~ +/- 18
    N =  90 d   se ~ +/- 10
    N = 180 d   se ~ +/-  3

By ~90 days the correction is pinned tighter than any consumer device will ever
be. This is why WINDOW_DAYS is 90: past that, extra precision is worth less than
the staleness it buys (see below).

===============================================================================
NEGATIVE RESULTS — things already tried that DO NOT work. Do not redo them.
===============================================================================

1. **Confounders do not help.** `bowel_movement`, `total_carbs_g`,
   `total_sodium_mg` and `body_water_pct` were each added as regressors on daily
   weight change. All of them explained ~0% of the variance and every one made
   residual sd slightly WORSE once degrees of freedom were paid for
   (0.4134 -> 0.4199..0.4268). The day-to-day wobble is real physiology we cannot
   observe with the columns we have. Do not build features around these.

2. **The two devices cannot be separated from daily regression.** Regressing daily
   weight change on `total_cals_in` and `total_cals_out` separately gave
   +1.27 +/- 2.71 and +2.28 +/- 3.75, where theory says +1.0 and -1.0. Tightening
   those CIs to a useful +/-0.2 needs on the order of 5,900 days (~16 years). That
   route is closed.

   The open route is **period contrast**: compare two long windows with different
   mean `total_cals_out`. If the bias is constant across them the error is additive
   (points at the food scan); if it scales with expenditure the error is
   multiplicative (points at Fitbit). With ~700 kcal/day difference in mean
   expenditure, a 10% Fitbit error shows up as a ~70 kcal/day gap in bias —
   detectable at ~2.7 sigma once each window has ~60 days. THIS REQUIRES ACTIVITY
   TO VARY; do not "helpfully" normalise it away.

3. **`templates` cannot validate the food model.** The 11 template-matched meals
   copy the template's stored macros verbatim (measured error exactly 0.0% on all
   11, by construction). They are substitutions, not predictions. To make them a
   real test the estimator's own guess would have to be recorded alongside the
   weighed truth for the same meal.

===============================================================================
WHICH SIDE THE CORRECTION IS APPLIED TO, AND WHY IT IS NEITHER
===============================================================================

For predicting weight it is algebraically irrelevant: energy_balance is
`cals_in - cals_out`, so +250 on the expenditure side is identical to -250 on the
intake side. But the raw columns are left UNTOUCHED, and a new derived column
carries the correction, because:

  * `total_cals_in` is a PARENT node — the roll-up that `total_protein_g`,
    `total_carbs_g`, `total_fat_g` and ~30 micronutrient totals hang off. Scaling
    calories without scaling the macros breaks their internal consistency; scaling
    all of them asserts the error is uniform across every nutrient, which is almost
    certainly false (a missed tablespoon of olive oil skews fat, not vitamin C).
  * `total_cals_out` is a leaf — but overwriting it destroys the very quantity the
    period-contrast test in negative result #2 needs.
  * Raw values must stay intact to detect a device actually breaking.

So `energy_balance_kcal` stays raw and `energy_balance_adj_kcal` is published
alongside it. Consumers that care about reality should read the adjusted column;
anything auditing the devices should read the raw one.

===============================================================================
THE FOUR RULES  (breaking any of these makes the system worse than useless)
===============================================================================

1. **Rolling window, not all-time.** All-time maximises precision but assumes the
   bias never drifts. A new tracker, a seasonal activity change or a diet change
   all move it. 90 days already buys +/-10 kcal/day; spending more precision to buy
   staleness is a bad trade.

2. **Shrink toward zero when the estimate is weak.** `shrink = P^2/(P^2 + se^2)`
   with P = PRIOR_SD_KCAL. At N=14 this applies ~78% of the raw estimate; by N=60
   it applies ~100%. Preferred over a hard significance gate because it degrades
   gracefully — it never fires a wild correction off ten days of data, and never
   withholds a well-measured one.

3. **Estimate on the PAST, apply to the FUTURE. Never refit on the window being
   evaluated.** `calibration_series` deliberately estimates each day's bias from
   rows STRICTLY BEFORE that day. Refit in-sample and the corrected balance matches
   the scale by construction, the residuals go to zero, and the system permanently
   loses the ability to detect a device failing. This is the single easiest way to
   silently destroy the value of this module.

4. **Surface drift, do not absorb it.** If the recent-window bias departs from the
   long-window bias, something CHANGED — a device drifting, a habit shifting. The
   `status` column flags it. A calibration layer that silently swallows every
   discrepancy is a machine for hiding exactly the failure it was built to detect.

===============================================================================
KNOWN LIMITATIONS
===============================================================================

* RHO = 7700 kcal/kg is the pure-fat figure. Losing a fat/lean mix makes the true
  value lower. A wrong RHO produces an error proportional to deficit SIZE, whereas
  the bias is constant — so they are separable with enough data, but we currently
  fold any RHO error into the bias. Safe while the deficit stays near its present
  ~760 kcal/day; revisit if it changes substantially.
* The drift test compares nested windows (30d is inside 90d), so the two estimates
  are correlated and the 2-sigma threshold is approximate, not an exact test.
* Backtest evidence is encouraging but not proof: rolling-origin over 12
  overlapping folds, the correction won 10/12 and cut mean absolute 7-day forecast
  error from 0.300 to 0.227 kg (-24%). The folds overlap, so treat as directional.

Origin: analysis run 2026-08-18 against the first 33 days of data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Energy density of tissue change. The textbook figure for adipose tissue; see
# KNOWN LIMITATIONS above before changing it.
RHO_KCAL_PER_KG = 7700.0

# Trailing window the published correction is fitted over. See rule 1.
WINDOW_DAYS = 90

# Short window used only to detect drift against the long one. See rule 4.
DRIFT_WINDOW_DAYS = 30

# Prior spread for the shrinkage in rule 2, in kcal/day. Read as: "before seeing
# any data, a combined device error beyond a few hundred kcal/day would surprise
# me". 300 is deliberately generous — it barely shrinks a well-measured estimate
# (98% retained at N=32) while heavily damping a noisy one.
PRIOR_SD_KCAL = 300.0

# Below this many weigh-ins a trend is meaningless and nothing is published.
MIN_DAYS = 10

# Drift flag threshold, in combined standard errors.
DRIFT_SIGMA = 2.0

CALIBRATION_TAB = "calibration"

CALIBRATION_HEADERS = [
    "date", "n_days", "window_start", "window_end",
    "observed_kg", "predicted_kg", "bias_raw_kcal", "se_kcal",
    "ci_low_kcal", "ci_high_kcal", "shrink", "bias_applied_kcal",
    "status", "note",
]


def _num(value: Any) -> Optional[float]:
    """Blank-safe float. Mirrors src/analysis.py — booleans are NOT numbers here
    (a TRUE in a numeric column is a data bug, not a 1)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class Calibration:
    """One fitted correction. `bias_applied_kcal` is the number to add to
    `energy_balance_kcal`; everything else exists so a human or an agent can see
    how much to trust it."""
    n_days: int
    window_start: str
    window_end: str
    observed_kg: float
    predicted_kg: float
    bias_raw_kcal: float
    se_kcal: float
    shrink: float
    bias_applied_kcal: float
    status: str
    note: str

    @property
    def ci(self) -> Tuple[float, float]:
        half = 1.96 * self.se_kcal
        return (self.bias_raw_kcal - half, self.bias_raw_kcal + half)

    def as_row(self, on_date: str) -> List[Any]:
        lo, hi = self.ci
        return [
            on_date, self.n_days, self.window_start, self.window_end,
            round(self.observed_kg, 3), round(self.predicted_kg, 3),
            round(self.bias_raw_kcal), round(self.se_kcal),
            round(lo), round(hi), round(self.shrink, 3),
            round(self.bias_applied_kcal), self.status, self.note,
        ]


def _series(daily: Sequence[Dict[str, Any]]) -> List[Tuple[str, Optional[float], Optional[float]]]:
    """(date, weight, energy_balance) per row, date-sorted. Rows are kept even when
    a value is missing so the day index stays a real calendar offset — dropping a
    blank day would silently compress the time axis and bias the slope."""
    out = [
        (str(r.get("date", "")), _num(r.get("weight_kg")),
         _num(r.get("energy_balance_kcal")))
        for r in daily
    ]
    return sorted((r for r in out if r[0]), key=lambda r: r[0])


def estimate(rows: Sequence[Tuple[str, Optional[float], Optional[float]]]) -> Optional[Calibration]:
    """Fit the bias over one window of (date, weight, energy_balance) tuples.

    The causal offset matters and is easy to get backwards: `weight_kg` is measured
    on the MORNING of its row, before that day's food, while `energy_balance_kcal`
    covers the waking day that FOLLOWS it. So the weight change from row 0's
    morning to row N-1's morning is driven by the energy balance of rows 0..N-2 —
    the last row's balance has not landed on any weigh-in yet and must be excluded.
    (`schema/registry.py` states the same thing on the column itself.)
    """
    weighed = [(i, w) for i, (_, w, _) in enumerate(rows) if w is not None]
    if len(weighed) < MIN_DAYS:
        return None

    # Fit on the weigh-ins we actually have, positioned by true day index so gaps
    # do not distort the slope.
    idx = [i for i, _ in weighed]
    vals = [w for _, w in weighed]
    span = idx[-1] - idx[0]
    if span < 1:
        return None

    n = len(vals)
    mx, my = mean(idx), mean(vals)
    sxx = sum((x - mx) ** 2 for x in idx)
    slope = sum((x - mx) * (y - my) for x, y in zip(idx, vals)) / sxx
    if n > 2:
        resid = [y - (my + slope * (x - mx)) for x, y in zip(idx, vals)]
        se_slope = math.sqrt(sum(r * r for r in resid) / (n - 2) / sxx)
    else:
        se_slope = 0.0

    observed_kg = slope * span

    # Energy balance strictly before the final weigh-in (see docstring above).
    balances = [eb for _, _, eb in rows[idx[0]:idx[-1]] if eb is not None]
    if not balances:
        return None
    predicted_kg = sum(balances) / RHO_KCAL_PER_KG

    n_eb = len(balances)
    bias_raw = (observed_kg - predicted_kg) * RHO_KCAL_PER_KG / n_eb
    se_bias = se_slope * span * RHO_KCAL_PER_KG / n_eb

    # Rule 2: shrink toward zero in proportion to how badly measured we are.
    # MIN_DAYS >= 3 guarantees n > 2 above, so `se_bias` is always residual-based
    # and a zero here means a perfect straight-line fit — i.e. maximum confidence,
    # shrink -> 1.0. (It does NOT mean "unknown"; that case is excluded by the
    # MIN_DAYS gate, which is why that gate must not be lowered below 3.)
    shrink = PRIOR_SD_KCAL ** 2 / (PRIOR_SD_KCAL ** 2 + se_bias ** 2)

    status = "ok" if n >= 30 else "warming_up"
    note = (f"{n} weigh-ins over {span + 1} d; "
            f"{n_eb} day(s) of energy balance")
    return Calibration(
        n_days=n, window_start=rows[idx[0]][0], window_end=rows[idx[-1]][0],
        observed_kg=observed_kg, predicted_kg=predicted_kg,
        bias_raw_kcal=bias_raw, se_kcal=se_bias, shrink=shrink,
        bias_applied_kcal=shrink * bias_raw, status=status, note=note,
    )


def _drift(long: Calibration, short: Optional[Calibration]) -> Optional[str]:
    """Rule 4. Flag when the recent window disagrees with the long one.

    The windows are nested (the short one is inside the long one), so the two
    estimates are correlated and this is a rough screen rather than an exact test.
    It is deliberately a FLAG, not an adjustment: the point is to make a device
    going wrong visible, not to quietly absorb it."""
    if short is None or short.se_kcal <= 0 or long.se_kcal <= 0:
        return None
    gap = abs(short.bias_raw_kcal - long.bias_raw_kcal)
    combined = math.sqrt(short.se_kcal ** 2 + long.se_kcal ** 2)
    if gap > DRIFT_SIGMA * combined:
        return (f"DRIFT: last {DRIFT_WINDOW_DAYS}d bias "
                f"{short.bias_raw_kcal:+.0f} vs {long.bias_raw_kcal:+.0f} "
                f"long-window ({gap / combined:.1f} sigma) — check devices/habits")
    return None


def current(daily: Sequence[Dict[str, Any]]) -> Optional[Calibration]:
    """The correction as of the most recent data, with drift folded into status.

    This is the one to call for "what is my bias right now?". It DOES use the full
    trailing window including the latest day, which is correct for reporting but
    would violate rule 3 if used to adjust a day inside that window — use
    `calibration_series` for that."""
    rows = _series(daily)
    if not rows:
        return None
    long = estimate(rows[-WINDOW_DAYS:])
    if long is None:
        return None
    warn = _drift(long, estimate(rows[-DRIFT_WINDOW_DAYS:]))
    if warn:
        return Calibration(**{**long.__dict__, "status": "drift", "note": warn})
    return long


def calibration_series(daily: Sequence[Dict[str, Any]]) -> List[List[Any]]:
    """One row per day: the bias that was knowable BEFORE that day began.

    Rule 3 lives here. For day i the window is rows[i-WINDOW_DAYS : i] — strictly
    earlier days. That makes every published `bias_applied_kcal` a genuine
    out-of-sample forecast for its own row, so comparing the adjusted balance
    against what the scale went on to do stays an honest test.

    Early rows have too little history and correctly get nothing.
    """
    rows = _series(daily)
    out: List[List[Any]] = []
    for i, (day, _, _) in enumerate(rows):
        window = rows[max(0, i - WINDOW_DAYS):i]
        cal = estimate(window)
        if cal is None:
            out.append([day, 0, "", "", "", "", "", "", "", "", "", "",
                        "insufficient_history",
                        f"need >= {MIN_DAYS} prior weigh-ins"])
            continue
        warn = _drift(cal, estimate(rows[max(0, i - DRIFT_WINDOW_DAYS):i]))
        if warn:
            cal = Calibration(**{**cal.__dict__, "status": "drift", "note": warn})
        out.append(cal.as_row(day))
    return out


def adjusted_balances(daily: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """date -> `energy_balance_adj_kcal`, using each day's out-of-sample bias.

    Days with no raw balance, or with no usable prior history, are omitted rather
    than written as the uncorrected value — a silently-uncorrected number that
    looks corrected is worse than a blank."""
    rows = _series(daily)
    series = calibration_series(daily)
    applied = {r[0]: r[11] for r in series if r[11] != ""}
    return {
        day: round(eb + applied[day])
        for day, _, eb in rows
        if eb is not None and day in applied
    }
