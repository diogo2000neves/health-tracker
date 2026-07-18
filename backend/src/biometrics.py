"""Turn Google Health payloads into ``daily_summary`` columns (Fitbit Air).

Pure transforms — no network, no sheet — so every rule below is unit-tested.
``src/google_health.py`` fetches; this decides what a day's numbers mean.

Three shapes arrive, and each keys its day differently:

* **Sleep sessions** — a session spans midnight, so it belongs to the day the user
  *woke up on* (the API agrees: it filters sleep on `civil_end_time`). Naps are
  excluded from the night figures and totalled separately; without that, a 2-hour
  afternoon nap lands on the same wake-day as the previous night and silently
  corrupts it.
* **Daily summaries** — already carry a civil `date`; taken as-is.
* **Rollups** — already aggregated per civil day by the server.

Two traps this module exists to absorb:

1. **Numbers arrive as JSON strings.** `minutesAsleep` is `"525"`, `countSum` is
   `"4151"`, durations are `"10620s"`. `float()` on those works, but a silent
   `None` or a stray unit would zero a column — everything goes through `_num`.
2. **Sleep intervals carry no civil time**, only `startTime` + `startUtcOffset`,
   so the local day has to be derived. Never `[:10]` the UTC timestamp: a 23:03Z
   bedtime is already the next local day in Lisbon.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from schema.registry import names_in

# -- column groups -------------------------------------------------------------
# Read from the registry rather than restated here, so this module physically
# cannot fill a column the schema doesn't declare (or miss one it does).
SLEEP_COLUMNS = names_in("sleep")          # night that ENDED on this date
RECOVERY_COLUMNS = names_in("recovery")    # overnight daily-* summaries
ACTIVITY_COLUMNS = names_in("activity")    # per civil day, via dailyRollUp

BIOMETRIC_COLUMNS = SLEEP_COLUMNS + RECOVERY_COLUMNS + ACTIVITY_COLUMNS


# -- primitives ----------------------------------------------------------------
def _num(value: Any) -> Optional[float]:
    """Coerce an API number, which may be a JSON string ("525"). None when absent,
    unparseable or not a real number — never 0, so a missing metric stays blank
    instead of pretending to be a genuine zero reading.

    NaN is a live case, not a theoretical one: the API returns the *string* `"NaN"`
    for a metric it cannot compute yet (`baselineTemperatureCelsius` needs 30 days
    of history). `float("NaN")` succeeds, so without the isfinite guard that
    sails through every type check and lands as a literal NaN in the sheet.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _duration_s(value: Any) -> Optional[float]:
    """Protobuf duration ("10620s") -> seconds."""
    if value is None:
        return None
    return _num(str(value).rstrip("s"))


def _iso_date(d: Dict[str, Any]) -> str:
    """A Google `Date` {year, month, day} -> "YYYY-MM-DD" ("" when incomplete)."""
    if not all(k in d for k in ("year", "month", "day")):
        return ""
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def _local(ts: str, offset: Any) -> Optional[datetime]:
    """RFC3339 UTC timestamp + utcOffset ("3600s") -> local wall-clock datetime.

    Sleep intervals ship no civil time, so this is the only way to get the day the
    user actually experienced. A 23:03Z bedtime with +1h is already tomorrow."""
    if not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = _duration_s(offset) or 0
    return moment + timedelta(seconds=seconds)


def _round(value: Optional[float], digits: int = 0) -> Optional[float]:
    if value is None:
        return None
    out = round(value, digits)
    return int(out) if digits == 0 else out


# -- sleep ---------------------------------------------------------------------
def _stage_minutes(summary: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """stagesSummary[] -> {STAGE: {"minutes": m, "count": c}}."""
    out: Dict[str, Dict[str, float]] = {}
    for stage in summary.get("stagesSummary") or []:
        name = str(stage.get("type") or "").upper()
        if name:
            out[name] = {"minutes": _num(stage.get("minutes")) or 0.0,
                         "count": _num(stage.get("count")) or 0.0}
    return out


def daily_sleep(points: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """wake-day -> sleep columns.

    Naps (`metadata.nap`) never touch the night figures — they only add to
    `nap_mins`. If several non-nap sessions end on one day (a fragmented night),
    the longest is treated as *the* night and the others fold into nap_mins, so
    the night columns always describe one coherent sleep.
    """
    nights: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    naps: Dict[str, float] = defaultdict(float)

    for point in points:
        sleep = point.get("sleep") or {}
        interval = sleep.get("interval") or {}
        end = _local(interval.get("endTime", ""), interval.get("endUtcOffset"))
        if end is None:
            continue
        day = end.date().isoformat()
        summary = sleep.get("summary") or {}
        asleep = _num(summary.get("minutesAsleep")) or 0.0
        if (sleep.get("metadata") or {}).get("nap"):
            naps[day] += asleep
            continue
        start = _local(interval.get("startTime", ""), interval.get("startUtcOffset"))
        nights[day].append({"sleep": sleep, "start": start, "end": end,
                            "asleep": asleep})

    out: Dict[str, Dict[str, Any]] = {}
    for day in set(nights) | set(naps):
        row: Dict[str, Any] = {}
        sessions = sorted(nights.get(day, []), key=lambda s: s["asleep"],
                          reverse=True)
        extra_naps = naps.get(day, 0.0)
        if sessions:
            main, rest = sessions[0], sessions[1:]
            extra_naps += sum(s["asleep"] for s in rest)  # shorter sleeps = naps
            summary = main["sleep"].get("summary") or {}
            in_bed = _num(summary.get("minutesInSleepPeriod"))
            asleep = _num(summary.get("minutesAsleep"))
            stages = _stage_minutes(summary)
            row.update({
                "sleep_start": main["start"].strftime("%H:%M") if main["start"] else None,
                "sleep_end": main["end"].strftime("%H:%M"),
                "time_in_bed_mins": _round(in_bed),
                "sleep_mins": _round(asleep),
                "sleep_latency_mins": _round(_num(summary.get("minutesToFallAsleep"))),
                "sleep_awake_mins": _round(_num(summary.get("minutesAwake"))),
                "sleep_deep_mins": _round(stages.get("DEEP", {}).get("minutes")),
                "sleep_rem_mins": _round(stages.get("REM", {}).get("minutes")),
                "sleep_light_mins": _round(stages.get("LIGHT", {}).get("minutes")),
                "sleep_awakenings": _round(stages.get("AWAKE", {}).get("count")),
            })
            # Efficiency is what Fitbit's proprietary "sleep score" mostly is, and
            # unlike the score it is honest arithmetic we can show our work for.
            if in_bed and asleep is not None and in_bed > 0:
                row["sleep_efficiency_pct"] = _round(asleep / in_bed * 100, 1)
        if extra_naps:
            row["nap_mins"] = _round(extra_naps)
        if row:
            out[day] = row
    return out


# -- recovery (daily-* summaries) ----------------------------------------------
def daily_recovery(by_type: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """{data_type: points} -> date -> recovery columns."""
    out: Dict[str, Dict[str, Any]] = defaultdict(dict)

    def each(data_type: str, key: str):
        for point in by_type.get(data_type, []):
            metric = point.get(key) or {}
            day = _iso_date(metric.get("date") or {})
            if day:
                yield day, metric

    for day, m in each("daily-resting-heart-rate", "dailyRestingHeartRate"):
        out[day]["resting_hr_bpm"] = _round(_num(m.get("beatsPerMinute")))

    for day, m in each("daily-heart-rate-variability", "dailyHeartRateVariability"):
        # 2 dp keeps the API's own precision (70.55 ms) while clipping float noise;
        # HRV trends are read in single milliseconds, so don't round harder.
        out[day].update({
            "hrv_ms": _round(_num(m.get("averageHeartRateVariabilityMilliseconds")), 2),
            "hrv_deep_sleep_ms": _round(_num(m.get(
                "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")), 2),
            "hrv_entropy": _round(_num(m.get("entropy")), 3),
            "non_rem_hr_bpm": _round(_num(m.get("nonRemHeartRateBeatsPerMinute"))),
        })

    for day, m in each("daily-oxygen-saturation", "dailyOxygenSaturation"):
        out[day].update({
            "spo2_pct": _round(_num(m.get("averagePercentage")), 1),
            "spo2_lower_pct": _round(_num(m.get("lowerBoundPercentage")), 1),
            "spo2_upper_pct": _round(_num(m.get("upperBoundPercentage")), 1),
        })

    for day, m in each("daily-respiratory-rate", "dailyRespiratoryRate"):
        out[day]["respiratory_rate_brpm"] = _round(_num(m.get("breathsPerMinute")), 1)

    for day, m in each("daily-sleep-temperature-derivations",
                       "dailySleepTemperatureDerivations"):
        nightly = _num(m.get("nightlyTemperatureCelsius"))
        baseline = _num(m.get("baselineTemperatureCelsius"))
        out[day]["skin_temp_c"] = _round(nightly, 2)
        # The deviation from your own 30-day baseline is the signal (illness,
        # alcohol, poor recovery); the absolute skin temperature alone is noise.
        if nightly is not None and baseline is not None:
            out[day]["skin_temp_dev"] = _round(nightly - baseline, 2)

    return {day: {k: v for k, v in cols.items() if v is not None}
            for day, cols in out.items()}


# -- activity (dailyRollUp) ----------------------------------------------------
def _rollup_day(point: Dict[str, Any]) -> str:
    return _iso_date((point.get("civilStartTime") or {}).get("date") or {})


def daily_activity(by_type: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """{data_type: rollup points} -> date -> activity columns."""
    out: Dict[str, Dict[str, Any]] = defaultdict(dict)

    def each(data_type: str, key: str):
        for point in by_type.get(data_type, []):
            day = _rollup_day(point)
            value = point.get(key)
            if day and value:
                yield day, value

    for day, v in each("steps", "steps"):
        out[day]["steps"] = _round(_num(v.get("countSum")))

    for day, v in each("distance", "distance"):
        mm = _num(v.get("millimetersSum"))
        if mm is not None:
            out[day]["distance_km"] = _round(mm / 1_000_000, 2)

    for day, v in each("total-calories", "totalCalories"):
        out[day]["total_cals_out"] = _round(_num(v.get("kcalSum")))

    for day, v in each("active-energy-burned", "activeEnergyBurned"):
        out[day]["active_cals"] = _round(_num(v.get("kcalSum")))

    for day, v in each("active-minutes", "activeMinutes"):
        total = 0.0
        for entry in v.get("activeMinutesRollupByActivityLevel") or []:
            mins = _num(entry.get("activeMinutesSum")) or 0.0
            level = str(entry.get("activityLevel") or "").lower()
            if level in ("light", "moderate", "vigorous"):
                out[day][f"active_mins_{level}"] = _round(mins)
            total += mins
        out[day]["total_active_mins"] = _round(total)

    for day, v in each("active-zone-minutes", "activeZoneMinutes"):
        out[day].update({
            "azm_fat_burn_mins": _round(_num(v.get("sumInFatBurnHeartZone"))),
            "azm_cardio_mins": _round(_num(v.get("sumInCardioHeartZone"))),
            "azm_peak_mins": _round(_num(v.get("sumInPeakHeartZone"))),
        })

    for day, v in each("sedentary-period", "sedentaryPeriod"):
        seconds = _duration_s(v.get("durationSum"))
        if seconds is not None:
            out[day]["sedentary_mins"] = _round(seconds / 60)

    for day, v in each("heart-rate", "heartRate"):
        out[day].update({
            "hr_min_bpm": _round(_num(v.get("beatsPerMinuteMin"))),
            "hr_avg_bpm": _round(_num(v.get("beatsPerMinuteAvg"))),
            "hr_max_bpm": _round(_num(v.get("beatsPerMinuteMax"))),
        })

    for day, v in each("time-in-heart-rate-zone", "timeInHeartRateZone"):
        for zone in v.get("timeInHeartRateZones") or []:
            name = str(zone.get("heartRateZone") or "").lower()
            seconds = _duration_s(zone.get("duration"))
            if name in ("light", "moderate", "vigorous", "peak") and seconds is not None:
                out[day][f"mins_hr_{name}"] = _round(seconds / 60)

    for day, v in each("swim-lengths-data", "swimLengthsData"):
        out[day]["swim_strokes"] = _round(_num(v.get("strokeCountSum")))

    return {day: {k: v for k, v in cols.items() if v is not None}
            for day, cols in out.items()}


# -- merge ---------------------------------------------------------------------
def biometric_days(sleep: Dict[str, Dict[str, Any]],
                   recovery: Dict[str, Dict[str, Any]],
                   activity: Dict[str, Dict[str, Any]],
                   start: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """date -> the biometric columns for that day.

    Returns a per-day mapping (not rows) so the caller can fold it together with
    the nutrition roll-up into ONE row per date. Two separate rows for the same
    date must never reach `upsert_daily`: it merges each against the same grid
    snapshot, so the second would clobber the first's columns — or, for a new
    date, append the day twice.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for day in sorted(set(sleep) | set(recovery) | set(activity)):
        if start and day < start:
            continue
        cols: Dict[str, Any] = {}
        cols.update(sleep.get(day, {}))
        cols.update(recovery.get(day, {}))
        cols.update(activity.get(day, {}))
        if cols:
            out[day] = cols
    return out
