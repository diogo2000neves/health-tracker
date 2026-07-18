"""Unit tests for the Fitbit Air -> daily_summary transforms (no network).

The fixtures are REAL payloads captured from the Google Health API for this user
on 2026-07-14..16, trimmed only for length — so the string-typed numbers, the
missing civil times and the nap flag are all exactly as the API sends them.
"""
from src.biometrics import (
    BIOMETRIC_COLUMNS, biometric_days, daily_activity, daily_recovery, daily_sleep,
)


def _sleep(start, end, summary, nap=False, offset="3600s"):
    point = {"sleep": {
        "interval": {"startTime": start, "endTime": end,
                     "startUtcOffset": offset, "endUtcOffset": offset},
        "type": "STAGES",
        "metadata": {"stagesStatus": "SUCCEEDED", "processed": True},
        "summary": summary,
    }}
    if nap:
        point["sleep"]["metadata"]["nap"] = True
    return point


# The real night of 15->16 July: 534 in bed, 525 asleep, 98.3% efficient.
_NIGHT = _sleep(
    "2026-07-15T22:31:00Z", "2026-07-16T07:25:00Z",
    {"minutesInSleepPeriod": "534", "minutesAfterWakeUp": "0",
     "minutesToFallAsleep": "0", "minutesAsleep": "525", "minutesAwake": "9",
     "stagesSummary": [
         {"type": "AWAKE", "minutes": "9", "count": "1"},
         {"type": "LIGHT", "minutes": "342", "count": "13"},
         {"type": "DEEP", "minutes": "85", "count": "5"},
         {"type": "REM", "minutes": "98", "count": "8"},
     ]})

# The real 2h nap on the afternoon of the 15th.
_NAP = _sleep(
    "2026-07-15T16:40:00Z", "2026-07-15T18:40:00Z",
    {"minutesInSleepPeriod": "120", "minutesAsleep": "111", "minutesAwake": "9",
     "minutesToFallAsleep": "0", "minutesAfterWakeUp": "0",
     "stagesSummary": [{"type": "DEEP", "minutes": "32", "count": "2"}]},
    nap=True)


# -- sleep ---------------------------------------------------------------------
def test_sleep_lands_on_the_wake_day_not_the_bedtime_day():
    # asleep 15th 23:31 local -> awake 16th 08:25 local. The row is the 16th:
    # that is the API's own convention (it filters on civil_end_time) and the day
    # the user actually experienced the rest.
    days = daily_sleep([_NIGHT])
    assert list(days) == ["2026-07-16"]
    row = days["2026-07-16"]
    assert row["sleep_start"] == "23:31"   # 22:31Z + 1h — never the raw UTC hour
    assert row["sleep_end"] == "08:25"


def test_sleep_reads_the_string_typed_summary():
    row = daily_sleep([_NIGHT])["2026-07-16"]
    assert row["time_in_bed_mins"] == 534   # the API sends "534", a JSON string
    assert row["sleep_mins"] == 525
    assert row["sleep_awake_mins"] == 9
    assert row["sleep_latency_mins"] == 0
    assert row["sleep_deep_mins"] == 85
    assert row["sleep_rem_mins"] == 98
    assert row["sleep_light_mins"] == 342
    assert row["sleep_awakenings"] == 1     # AWAKE *count*, not its minutes


def test_sleep_efficiency_is_derived():
    # 525/534 — this is the honest arithmetic that replaces Fitbit's proprietary
    # sleep score (which the API does not expose at all).
    assert daily_sleep([_NIGHT])["2026-07-16"]["sleep_efficiency_pct"] == 98.3


def test_a_nap_never_contaminates_the_night():
    # The real trap: this nap ENDS on the 15th, the same wake-day a night would
    # land on. Without the nap flag it would overwrite or merge into that night.
    days = daily_sleep([_NIGHT, _NAP])
    assert days["2026-07-15"] == {"nap_mins": 111}      # nap only, no night stats
    assert days["2026-07-16"]["sleep_mins"] == 525      # the night is untouched
    assert "nap_mins" not in days["2026-07-16"]


def test_the_longest_session_is_the_night_and_shorter_ones_become_naps():
    # A fragmented night: two non-nap sessions ending the same day. The night
    # columns must describe ONE coherent sleep, so the shorter folds into naps.
    short = _sleep("2026-07-16T05:00:00Z", "2026-07-16T06:00:00Z",
                   {"minutesInSleepPeriod": "60", "minutesAsleep": "50"})
    days = daily_sleep([short, _NIGHT])
    assert days["2026-07-16"]["sleep_mins"] == 525      # the 525m one wins
    assert days["2026-07-16"]["nap_mins"] == 50


def test_sleep_ignores_unusable_points():
    assert daily_sleep([{"sleep": {}}]) == {}
    assert daily_sleep([]) == {}


# -- recovery ------------------------------------------------------------------
def test_recovery_maps_every_daily_summary():
    by_type = {
        "daily-heart-rate-variability": [{"dailyHeartRateVariability": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "averageHeartRateVariabilityMilliseconds": 73.1,
            "nonRemHeartRateBeatsPerMinute": "58",
            "entropy": 3.106,
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 70.55,
        }}],
        "daily-resting-heart-rate": [{"dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "beatsPerMinute": "54",
        }}],
        "daily-oxygen-saturation": [{"dailyOxygenSaturation": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "averagePercentage": 95.4, "lowerBoundPercentage": 92.0,
            "upperBoundPercentage": 98.1,
        }}],
        "daily-respiratory-rate": [{"dailyRespiratoryRate": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "breathsPerMinute": 14.2,
        }}],
    }
    row = daily_recovery(by_type)["2026-07-16"]
    assert row["hrv_ms"] == 73.1
    assert row["hrv_deep_sleep_ms"] == 70.55
    assert row["hrv_entropy"] == 3.106
    assert row["non_rem_hr_bpm"] == 58          # "58" string -> number
    assert row["resting_hr_bpm"] == 54
    assert row["spo2_pct"] == 95.4
    assert row["spo2_lower_pct"] == 92.0
    assert row["respiratory_rate_brpm"] == 14.2


def test_skin_temp_deviation_is_derived_from_the_personal_baseline():
    by_type = {"daily-sleep-temperature-derivations": [
        {"dailySleepTemperatureDerivations": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "nightlyTemperatureCelsius": 34.8,
            "baselineTemperatureCelsius": 34.5,
        }}]}
    row = daily_recovery(by_type)["2026-07-16"]
    assert row["skin_temp_c"] == 34.8
    assert row["skin_temp_dev"] == 0.3      # the deviation is the actual signal


def test_skin_temp_dev_omitted_without_a_baseline():
    by_type = {"daily-sleep-temperature-derivations": [
        {"dailySleepTemperatureDerivations": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "nightlyTemperatureCelsius": 34.8,
        }}]}
    assert "skin_temp_dev" not in daily_recovery(by_type)["2026-07-16"]


def test_the_string_nan_baseline_never_reaches_the_sheet():
    # EXACTLY what the API sends before 30 days of history exist: the *string*
    # "NaN". float("NaN") succeeds, so this slips past a naive type check and
    # writes a literal NaN into the sheet. The nightly reading is still good.
    by_type = {"daily-sleep-temperature-derivations": [
        {"dailySleepTemperatureDerivations": {
            "date": {"year": 2026, "month": 7, "day": 16},
            "nightlyTemperatureCelsius": 34.32994350282487,
            "baselineTemperatureCelsius": "NaN",
            "relativeNightlyStddev30dCelsius": "NaN",
        }}]}
    row = daily_recovery(by_type)["2026-07-16"]
    assert row == {"skin_temp_c": 34.33}       # dev omitted, not NaN
    assert "skin_temp_dev" not in row


def test_num_rejects_nan_and_infinity():
    from src.biometrics import _num
    assert _num("NaN") is None
    assert _num(float("nan")) is None
    assert _num(float("inf")) is None
    assert _num("525") == 525.0                # the normal string-int case
    assert _num(0) == 0.0                      # a real zero survives
    assert _num(None) is None
    assert _num(True) is None                  # bool is an int subclass


# -- activity (dailyRollUp) -----------------------------------------------------
def _rollup(day, key, value):
    return {"civilStartTime": {"date": {"year": 2026, "month": 7, "day": day}},
            key: value}


def test_activity_maps_the_rollups():
    by_type = {
        "steps": [_rollup(15, "steps", {"countSum": "4151"})],
        "distance": [_rollup(15, "distance", {"millimetersSum": "3058000"})],
        "total-calories": [_rollup(15, "totalCalories", {"kcalSum": 2221.364542})],
        "active-energy-burned": [_rollup(15, "activeEnergyBurned",
                                         {"kcalSum": 228.301374})],
        "sedentary-period": [_rollup(15, "sedentaryPeriod", {"durationSum": "39600s"})],
        "heart-rate": [_rollup(15, "heartRate", {"beatsPerMinuteMin": 50,
                                                 "beatsPerMinuteAvg": 70.31575,
                                                 "beatsPerMinuteMax": 129})],
        "swim-lengths-data": [_rollup(15, "swimLengthsData", {"strokeCountSum": "31"})],
    }
    row = daily_activity(by_type)["2026-07-15"]
    assert row["steps"] == 4151                 # "4151" string -> number
    assert row["distance_km"] == 3.06           # millimetres -> km
    assert row["total_cals_out"] == 2221        # only reachable via dailyRollUp
    assert row["active_cals"] == 228
    assert row["sedentary_mins"] == 660         # "39600s" -> 660 min
    assert row["hr_min_bpm"] == 50 and row["hr_max_bpm"] == 129
    assert row["hr_avg_bpm"] == 70
    assert row["swim_strokes"] == 31


def test_activity_splits_minutes_by_level_and_zone():
    by_type = {
        "active-minutes": [_rollup(15, "activeMinutes", {
            "activeMinutesRollupByActivityLevel": [
                {"activityLevel": "LIGHT", "activeMinutesSum": "159"},
                {"activityLevel": "MODERATE", "activeMinutesSum": "12"},
            ]})],
        "active-zone-minutes": [_rollup(15, "activeZoneMinutes", {
            "sumInFatBurnHeartZone": "1", "sumInCardioHeartZone": "0",
            "sumInPeakHeartZone": "0"})],
        "time-in-heart-rate-zone": [_rollup(15, "timeInHeartRateZone", {
            "timeInHeartRateZones": [
                {"heartRateZone": "LIGHT", "duration": "85260s"},
                {"heartRateZone": "MODERATE", "duration": "60s"},
            ]})],
    }
    row = daily_activity(by_type)["2026-07-15"]
    assert row["active_mins_light"] == 159
    assert row["active_mins_moderate"] == 12
    assert row["total_active_mins"] == 171      # summed across levels
    assert row["azm_fat_burn_mins"] == 1
    assert row["mins_hr_light"] == 1421         # 85260s -> minutes
    assert row["mins_hr_moderate"] == 1


def test_activity_ignores_days_the_tracker_produced_nothing():
    assert daily_activity({"steps": []}) == {}
    # a rollup window with no value object for the type is skipped, not zeroed
    assert daily_activity({"steps": [{"civilStartTime": {
        "date": {"year": 2026, "month": 7, "day": 15}}}]}) == {}


# -- merge ----------------------------------------------------------------------
def test_biometric_days_merges_the_three_groups_per_date():
    days = biometric_days(
        {"2026-07-16": {"sleep_mins": 525}},
        {"2026-07-16": {"hrv_ms": 73.1}},
        {"2026-07-16": {"steps": 728}, "2026-07-15": {"steps": 4151}},
    )
    assert days["2026-07-16"] == {"sleep_mins": 525, "hrv_ms": 73.1, "steps": 728}
    assert days["2026-07-15"] == {"steps": 4151}


def test_biometric_days_respects_the_window():
    days = biometric_days({}, {}, {"2026-07-10": {"steps": 1}, "2026-07-16": {"steps": 2}},
                          start="2026-07-15")
    assert list(days) == ["2026-07-16"]


def test_biometric_columns_have_no_duplicates():
    assert len(BIOMETRIC_COLUMNS) == len(set(BIOMETRIC_COLUMNS))
