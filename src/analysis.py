"""Derived views that make `daily_summary` safe and easy to reason about.

Two tabs, both rebuilt from scratch on every daily run (like `dashboard`), so they
can never drift out of step with the observations they derive from:

* **`analysis`** — the causally aligned view. This exists because
  `daily_summary` is an *observation* table, not a causal one, and correlating it
  naively gives answers that are backwards in time.
* **`baselines`** — what "normal" looks like *for this person*, so an absolute
  number becomes interpretable.

## Why `analysis` has to exist

A `daily_summary` row for day N mixes three different time windows:

    sleep / recovery   -> the night that ENDED on the morning of N
    weight             -> measured on the morning of N, fasted
    food / activity    -> happened DURING day N, after both of the above

So the food on row N is eaten *after* the sleep on row N and *after* the weigh-in
on row N. Ask "does `total_cals_in` correlate with `sleep_efficiency_pct`?" on the
raw table and you are asking whether **tomorrow's dinner affected last night's
sleep**. The honest pairing is inputs from day N against outcomes from day N+1,
which is what this module builds — driven entirely by each column's declared
`causal` window in the registry, not by a hand-maintained list.

Storage stays honest (every value stamped when it was measured, which is
unambiguous and survives a migration); the alignment lives here, in the view.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Sequence

from schema.registry import (
    BY_NAME, CAUSAL_LABELS, Column, causal_inputs, causal_outcomes,
    numeric_columns,
)

# How many days back the personal baselines look. 28 days ~= 4 weeks, so it spans
# whole weeks and isn't skewed by "weekends are different".
BASELINE_DAYS = 28

# Outcome columns get this suffix in the analysis tab: they are read from the day
# AFTER the inputs on the same row.
NEXT_SUFFIX = "_next"


def _num(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


# -- analysis (causally aligned) ----------------------------------------------
def analysis_headers() -> List[str]:
    """`date`, then everything I did on that day, then what my body did next."""
    return (["date"]
            + [c.name for c in causal_inputs()]
            + [c.name + NEXT_SUFFIX for c in causal_outcomes()])


def analysis_rows(daily: Sequence[Dict[str, Any]]) -> List[List[Any]]:
    """Inputs from day N beside the outcomes they plausibly caused, on day N+1.

    Keyed on real dates, not row adjacency: a gap in the data must not silently
    pair Monday's food with Friday's sleep. A day whose successor is missing keeps
    its inputs and leaves the outcomes blank.
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for row in daily:
        key = str(row.get("date") or "")
        if key:
            by_date[key] = row

    inputs, outcomes = causal_inputs(), causal_outcomes()
    rows: List[List[Any]] = []
    for day in sorted(by_date):
        try:
            nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        except ValueError:
            continue  # not a real date — skip rather than mispair
        today, tomorrow = by_date[day], by_date.get(nxt, {})
        # Skip days that carry no inputs at all: a row that only holds outcomes
        # would add an empty line whose outcomes already appear on the day before.
        if not any(today.get(c.name) not in (None, "") for c in inputs):
            continue
        rows.append(
            [day]
            + [today.get(c.name, "") for c in inputs]
            + [tomorrow.get(c.name, "") for c in outcomes]
        )
    return rows


def analysis_legend() -> List[str]:
    """A one-line note explaining the tab, written above the header."""
    return [
        "CAUSALLY ALIGNED VIEW — rebuilt every run, do not edit. Each row pairs "
        "what I did on `date` with what my body did AFTERWARDS: columns ending "
        f"`{NEXT_SUFFIX}` are read from the FOLLOWING day's row. Correlate across "
        "this tab, not daily_summary, or you will be asking whether tomorrow's "
        "dinner affected last night's sleep."
    ]


# -- baselines (what is normal for THIS person) --------------------------------
BASELINE_HEADERS = [
    "metric", "block", "unit", "direction", "n", "mean", "sd", "min", "max",
    "latest", "latest_z", "interpretation",
]


def _z(latest: Optional[float], avg: Optional[float],
       sd: Optional[float]) -> Optional[float]:
    if latest is None or avg is None or not sd:
        return None
    return round((latest - avg) / sd, 2)


def _interpret(col: Column, z: Optional[float]) -> str:
    """Turn a z-score into the sentence an AI would otherwise have to guess at."""
    if z is None:
        return "no baseline yet"
    size = abs(z)
    if size < 1:
        return "typical for me"
    band = "unusually" if size >= 2 else "somewhat"
    high = z > 0
    if col.direction == "neutral":
        return f"{band} {'high' if high else 'low'} for me"
    good = (high and col.direction == "up_good") or (
        not high and col.direction == "down_good")
    return f"{band} {'high' if high else 'low'} for me ({'good' if good else 'worse than usual'})"


def baseline_rows(daily: Sequence[Dict[str, Any]],
                  today: Optional[date] = None,
                  window_days: int = BASELINE_DAYS) -> List[List[Any]]:
    """One row per numeric metric: what normal looks like over the trailing window.

    This is the cheapest possible fix for the biggest AI-readability problem in the
    data. `hrv_ms = 73.1` is uninterpretable on its own — 73 ms is excellent for one
    person and a red flag for another. Against a personal mean and SD it becomes
    "+0.8 SD, typical for me", which is a sentence you can actually reason from.
    Only metrics with at least 3 readings get a baseline; below that an SD is noise.
    """
    today = today or date.today()
    floor = (today - timedelta(days=window_days)).isoformat()
    recent = [r for r in daily if str(r.get("date", "")) >= floor]
    recent.sort(key=lambda r: str(r.get("date", "")))

    rows: List[List[Any]] = []
    for col in numeric_columns():
        values = [v for v in (_num(r.get(col.name)) for r in recent) if v is not None]
        if not values:
            continue
        latest = values[-1]
        avg = round(mean(values), 2)
        sd = round(pstdev(values), 2) if len(values) >= 3 else None
        z = _z(latest, avg, sd)
        rows.append([
            col.name, col.block, col.unit, col.direction, len(values),
            avg, sd if sd is not None else "", round(min(values), 2),
            round(max(values), 2), latest, z if z is not None else "",
            _interpret(col, z),
        ])
    return rows
