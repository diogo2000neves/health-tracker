"""The `baselines` tab: what "normal" looks like *for this person*.

Rebuilt from scratch on every daily run, so it can never drift out of step with the
observations it derives from.

An absolute reading is close to meaningless on its own — 73 ms of HRV is excellent
for one person and a warning for another — so this turns each metric into a mean,
a spread and a z-score over a trailing window, plus a plain sentence saying whether
the latest value is normal *for you*.

**Reading correlations out of `daily_summary` needs care**, and the tab that used
to do it for you is gone. A row is an *observation of a date*, not a causal unit:
sleep and recovery on row N happened the night BEFORE N, and the weigh-in was taken
that morning BEFORE the day's food. So pairing intake with same-row sleep asks
whether tomorrow's dinner affected last night's sleep. Every column's real
measurement window is declared in `schema/registry.py` and published in the sheet's
`schema` tab as `measures_when` — pair day N's intake against day N+1's outcomes.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional, Sequence

from schema.registry import Column, numeric_columns

# How many days back the personal baselines look. 28 days ~= 4 weeks, so it spans
# whole weeks and isn't skewed by "weekends are different".
BASELINE_DAYS = 28


def _num(value: Any) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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
