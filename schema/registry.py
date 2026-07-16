"""The single source of truth for the `daily_summary` schema.

Everything else is generated from this file: the sheet's header row and column
order, the `schema` tab, the plausibility bands the ingest service validates OCR
against, the causal alignment of the `analysis` tab, the JSON the iOS app reads,
and the Swift/SQL type exports. Change a column here and `python -m src.maintenance`
migrates the sheet to match.

Deliberately **pure stdlib** and dependency-free: it is copied into *both* container
images (the daily job and the ingest service, which cannot import `src/`). That
copy is what finally kills the hand-mirrored `BODY_METRICS` list that used to live
in two places and could silently drift apart.

## The four things every column declares, and why

* ``unit`` / ``dtype`` — a number without its unit is a trap. `skin_temp_c` and
  `skin_temp_dev` are both Celsius but mean completely different things.
* ``source`` — who owns the column. The merge-upsert lets each source write only
  its own columns, so this is the write contract, not just documentation.
* ``direction`` — whether higher is better. An AI cannot infer this: high
  `hrv_ms` is good, high `resting_hr_bpm` is bad, and `weight_kg` depends
  entirely on a goal the data doesn't contain. Where it's genuinely ambiguous we
  say `neutral` rather than guess.
* ``causal`` — **the important one.** *When the measured thing actually happened,
  relative to the row's date.* A row is an observation of a day, not a causal
  unit: the sleep on row N happened the night *before* N, and the food on row N is
  eaten *after* that sleep and after the morning weigh-in. Correlating them on the
  same row asks "did tomorrow's dinner affect last night's sleep?" — backwards in
  time. `src/analysis.py` uses this field to build a causally honest view.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# -- vocabularies --------------------------------------------------------------
# Blocks, in sheet order. These drive the collapsible column groups in the sheet
# and the nesting of the iOS app's JSON.
BLOCKS: List[str] = [
    "key", "self_report", "sleep", "recovery", "activity", "nutrition", "body",
    "meta",
]

BLOCK_LABELS: Dict[str, str] = {
    "key": "Key",
    "self_report": "Self-reported",
    "sleep": "Sleep (Fitbit, night that ended this morning)",
    "recovery": "Overnight recovery (Fitbit)",
    "activity": "Activity & energy (Fitbit)",
    "nutrition": "Nutrition (meals roll-up)",
    "body": "Body composition (scale screenshot)",
    "meta": "Bookkeeping",
}

# Who writes the column. Each source fills only its own columns (merge-upsert).
SOURCES = ("system", "user", "fitbit", "scale", "meals", "derived")

# Causal windows: when the measured phenomenon actually occurred.
WAKING_DAY = "waking_day"        # 05:00 this day -> 05:00 next (nutrition)
CALENDAR_DAY = "calendar_day"    # 00:00-24:00 this day (activity)
NIGHT_ENDING = "night_ending"    # the night that ended on this morning
MORNING_OF = "morning_of"        # measured this morning, fasted
DAY_OF = "day_of"                # self-reported about this day
NO_WINDOW = "none"               # key / bookkeeping

CAUSAL_LABELS: Dict[str, str] = {
    WAKING_DAY: "food eaten 05:00 this day -> 05:00 next day",
    CALENDAR_DAY: "00:00-24:00 on this date",
    NIGHT_ENDING: "the night that ended on this morning",
    MORNING_OF: "measured on this morning, fasted",
    DAY_OF: "reported about this date",
    NO_WINDOW: "not a measurement",
}

# An INPUT is something you did during day N. An OUTCOME is what your body did
# afterwards — it is caused by the *previous* day's inputs, which is exactly why
# it cannot be correlated against the same row's intake.
CAUSAL_INPUT = frozenset({WAKING_DAY, CALENDAR_DAY})
CAUSAL_OUTCOME = frozenset({NIGHT_ENDING, MORNING_OF, DAY_OF})

UP_GOOD, DOWN_GOOD, NEUTRAL = "up_good", "down_good", "neutral"


@dataclass(frozen=True)
class Column:
    name: str
    block: str
    dtype: str                       # date|datetime|time|number|integer|boolean|string
    unit: str                        # "" when unitless
    source: str
    causal: str
    direction: str
    description: str
    range: Optional[Tuple[float, float]] = None   # plausible human range
    precision: Optional[int] = None               # decimal places for display
    tier: int = 2                                 # 1 = headline metric

    @property
    def role(self) -> Optional[str]:
        if self.causal in CAUSAL_INPUT:
            return "input"
        if self.causal in CAUSAL_OUTCOME:
            return "outcome"
        return None


def _nutrient(name: str, unit: str, label: str, tier: int = 2) -> Column:
    """A Tier-1 micronutrient daily total, summed from each meal's items JSON."""
    return Column(
        name=f"total_{name}", block="nutrition", dtype="number", unit=unit,
        source="meals", causal=WAKING_DAY, direction=NEUTRAL, tier=tier,
        precision=1,
        description=f"Total {label} eaten across the waking day, summed from the "
                    f"per-ingredient breakdown in the meals tab.",
    )


# -- the schema ----------------------------------------------------------------
DAILY_COLUMNS: List[Column] = [
    Column("date", "key", "date", "", "system", NO_WINDOW, NEUTRAL, tier=1,
           description="The local civil day (Europe/Lisbon). Primary key: one row "
                       "per day, and the join key for every source."),

    # -- self-reported ---------------------------------------------------------
    Column("subjective_feel", "self_report", "number", "1-10", "user", DAY_OF,
           UP_GOOD, tier=1, range=(1, 10), precision=1,
           description="How I felt on this day, 1-10, logged via POST /feel. The "
                       "only wholly subjective signal; useful as ground truth for "
                       "whether the objective recovery metrics match lived experience."),
    Column("bowel_movement", "self_report", "boolean", "", "user", DAY_OF,
           NEUTRAL, tier=2,
           description="TRUE if I had a bowel movement on this day; blank means "
                       "none was logged (not necessarily none happened). Set from a "
                       "plain text note. Digestion lags intake by roughly a day."),

    # -- sleep (night that ENDED on this date) ---------------------------------
    # Headline metric first: each block's first column stays visible when the block
    # is collapsed in the sheet, so it is the one you see at a glance.
    Column("sleep_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           UP_GOOD, tier=1, range=(0, 1000),
           description="Minutes actually asleep. The headline sleep number."),
    Column("sleep_efficiency_pct", "sleep", "number", "%", "derived", NIGHT_ENDING,
           UP_GOOD, tier=1, range=(0, 100), precision=1,
           description="sleep_mins / time_in_bed_mins. Above ~90% is good. This is "
                       "the honest stand-in for Fitbit's proprietary sleep score, "
                       "which the Google Health API does not expose at all."),
    Column("sleep_deep_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           UP_GOOD, tier=1, range=(0, 400),
           description="Deep (slow-wave) sleep. Physical restoration; suppressed by "
                       "alcohol and late eating. Typically 13-23% of the night."),
    Column("sleep_rem_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           UP_GOOD, tier=1, range=(0, 400),
           description="REM sleep. Cognitive/emotional consolidation. Typically "
                       "20-25% of the night."),
    Column("time_in_bed_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2, range=(0, 1000),
           description="Total sleep period: asleep + awake time in bed."),
    Column("sleep_light_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2, range=(0, 700),
           description="Light sleep. The bulk of the night; not itself a quality "
                       "signal."),
    Column("sleep_awake_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           DOWN_GOOD, tier=2, range=(0, 500),
           description="Minutes awake during the sleep period (fragmentation)."),
    Column("sleep_latency_mins", "sleep", "integer", "min", "fitbit", NIGHT_ENDING,
           DOWN_GOOD, tier=2, range=(0, 300),
           description="Minutes taken to fall asleep. Long latency often follows a "
                       "late or heavy meal, caffeine, or a stressful day."),
    Column("sleep_awakenings", "sleep", "integer", "count", "fitbit", NIGHT_ENDING,
           DOWN_GOOD, tier=2, range=(0, 60),
           description="Number of distinct awake episodes during the night."),
    Column("sleep_start", "sleep", "time", "HH:MM", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2,
           description="Local clock time I fell asleep (usually the previous "
                       "evening). Consistency of this time matters more than its value."),
    Column("sleep_end", "sleep", "time", "HH:MM", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2,
           description="Local clock time I woke on this morning."),
    Column("nap_mins", "sleep", "integer", "min", "fitbit", DAY_OF, NEUTRAL,
           tier=2, range=(0, 600),
           description="Daytime sleep, kept deliberately apart from the night "
                       "figures: a nap ends on the same wake-day as a night and "
                       "would otherwise corrupt it. Note the window is this DAY, "
                       "unlike every other sleep column."),

    # -- overnight recovery ----------------------------------------------------
    Column("resting_hr_bpm", "recovery", "integer", "bpm", "fitbit", NIGHT_ENDING,
           DOWN_GOOD, tier=1, range=(25, 120),
           description="Resting heart rate, computed overnight. A multi-day rise is "
                       "the classic signature of under-recovery, illness, alcohol or "
                       "accumulated stress."),
    Column("hrv_ms", "recovery", "number", "ms", "fitbit", NIGHT_ENDING, UP_GOOD,
           tier=1, range=(1, 300), precision=2,
           description="Overnight heart-rate variability (RMSSD). Higher generally "
                       "means better parasympathetic recovery. Meaningless in "
                       "absolute terms — always read against my own baseline (see "
                       "the baselines tab)."),
    Column("hrv_deep_sleep_ms", "recovery", "number", "ms", "fitbit", NIGHT_ENDING,
           UP_GOOD, tier=2, range=(1, 300), precision=2,
           description="RMSSD measured during deep sleep only — less contaminated by "
                       "movement than the whole-night average."),
    Column("hrv_entropy", "recovery", "number", "", "fitbit", NIGHT_ENDING, UP_GOOD,
           tier=2, range=(0, 10), precision=3,
           description="Randomness of heartbeat intervals. Higher entropy tends to "
                       "indicate a more adaptable autonomic system."),
    Column("non_rem_hr_bpm", "recovery", "integer", "bpm", "fitbit", NIGHT_ENDING,
           DOWN_GOOD, tier=2, range=(25, 120),
           description="Average heart rate during non-REM sleep."),
    Column("spo2_pct", "recovery", "number", "%", "fitbit", NIGHT_ENDING, UP_GOOD,
           tier=1, range=(50, 100), precision=1,
           description="Average overnight blood-oxygen saturation. Healthy is ~95-100%; "
                       "sustained dips can indicate disturbed breathing."),
    Column("spo2_lower_pct", "recovery", "number", "%", "fitbit", NIGHT_ENDING,
           UP_GOOD, tier=2, range=(50, 100), precision=1,
           description="Lower bound of the overnight SpO2 confidence interval — this "
                       "is a distribution bound, not the night's minimum reading."),
    Column("spo2_upper_pct", "recovery", "number", "%", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2, range=(50, 100), precision=1,
           description="Upper bound of the overnight SpO2 confidence interval."),
    Column("respiratory_rate_brpm", "recovery", "number", "breaths/min", "fitbit",
           NIGHT_ENDING, NEUTRAL, tier=2, range=(4, 40), precision=1,
           description="Average overnight breathing rate. Stable per person; a rise "
                       "of 1-2 breaths/min often precedes illness."),
    Column("skin_temp_c", "recovery", "number", "C", "fitbit", NIGHT_ENDING,
           NEUTRAL, tier=2, range=(20, 45), precision=2,
           description="Absolute overnight skin temperature. On its own this is "
                       "noise — ambient conditions dominate. Use skin_temp_dev."),
    Column("skin_temp_dev", "recovery", "number", "C", "derived", NIGHT_ENDING,
           NEUTRAL, tier=1, range=(-10, 10), precision=2,
           description="Deviation of skin temperature from my own 30-day baseline. "
                       "THIS is the signal: a positive deviation tracks illness, "
                       "alcohol and poor recovery. Blank until Fitbit has 30 days of "
                       "history to build the baseline from."),

    # -- activity & energy -----------------------------------------------------
    Column("steps", "activity", "integer", "count", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=1, range=(0, 100000),
           description="Total steps taken on this calendar day."),
    Column("distance_km", "activity", "number", "km", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=2, range=(0, 200), precision=2,
           description="Distance covered on this calendar day."),
    Column("total_cals_out", "activity", "integer", "kcal", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=1, range=(500, 10000),
           description="Total energy expenditure: basal metabolism plus all "
                       "activity. Measured, not estimated. Pair with total_cals_in "
                       "for true energy balance — this is the number the whole "
                       "system exists to produce."),
    Column("active_cals", "activity", "integer", "kcal", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=2, range=(0, 8000),
           description="Energy burned above basal metabolism (the movement part of "
                       "total_cals_out)."),
    Column("total_active_mins", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=1, range=(0, 1440),
           description="Total active minutes across all intensity levels."),
    Column("active_mins_light", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Active minutes at light intensity."),
    Column("active_mins_moderate", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Active minutes at moderate intensity."),
    Column("active_mins_vigorous", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Active minutes at vigorous intensity."),
    Column("azm_fat_burn_mins", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Active Zone Minutes in the fat-burn heart-rate zone."),
    Column("azm_cardio_mins", "activity", "integer", "min", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=2, range=(0, 1440),
           description="Active Zone Minutes in the cardio heart-rate zone."),
    Column("azm_peak_mins", "activity", "integer", "min", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=2, range=(0, 1440),
           description="Active Zone Minutes in the peak heart-rate zone."),
    Column("sedentary_mins", "activity", "integer", "min", "fitbit", CALENDAR_DAY,
           DOWN_GOOD, tier=2, range=(0, 1440),
           description="Minutes spent sedentary (excludes sleep)."),
    Column("hr_min_bpm", "activity", "integer", "bpm", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=2, range=(25, 220),
           description="Lowest heart rate recorded across the day."),
    Column("hr_avg_bpm", "activity", "integer", "bpm", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=2, range=(25, 220),
           description="Average heart rate across the day."),
    Column("hr_max_bpm", "activity", "integer", "bpm", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=2, range=(25, 220),
           description="Peak heart rate recorded across the day."),
    Column("mins_hr_light", "activity", "integer", "min", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=2, range=(0, 1440),
           description="Minutes with heart rate in the light zone. Note this counts "
                       "most of a resting day, so it is a weak signal on its own."),
    Column("mins_hr_moderate", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Minutes with heart rate in the moderate zone."),
    Column("mins_hr_vigorous", "activity", "integer", "min", "fitbit",
           CALENDAR_DAY, UP_GOOD, tier=2, range=(0, 1440),
           description="Minutes with heart rate in the vigorous zone."),
    Column("mins_hr_peak", "activity", "integer", "min", "fitbit", CALENDAR_DAY,
           UP_GOOD, tier=2, range=(0, 1440),
           description="Minutes with heart rate in the peak zone."),
    Column("swim_strokes", "activity", "integer", "count", "fitbit", CALENDAR_DAY,
           NEUTRAL, tier=2, range=(0, 100000),
           description="Swim strokes counted on this day (blank on non-swim days)."),

    # -- nutrition -------------------------------------------------------------
    Column("energy_balance_kcal", "nutrition", "integer", "kcal", "derived",
           WAKING_DAY, NEUTRAL, tier=1, range=(-8000, 8000),
           description="total_cals_in - total_cals_out. Positive is a surplus, "
                       "negative a deficit. Blank unless both sides exist. The "
                       "single most important derived number here: body composition "
                       "should track this over weeks, and the outcome shows up on "
                       "the FOLLOWING day's weigh-in, not this row's."),
    Column("total_cals_in", "nutrition", "number", "kcal", "meals", WAKING_DAY,
           NEUTRAL, tier=1, range=(0, 15000), precision=1,
           description="Total energy eaten across the waking day (05:00 to 05:00, so "
                       "a midnight snack counts toward the day it belongs to). Only "
                       "written once the day is over — never a partial total."),
    Column("total_protein_g", "nutrition", "number", "g", "meals", WAKING_DAY,
           UP_GOOD, tier=1, range=(0, 1000), precision=1,
           description="Total protein. The macro that most affects whether weight "
                       "change is muscle or fat."),
    Column("total_carbs_g", "nutrition", "number", "g", "meals", WAKING_DAY,
           NEUTRAL, tier=1, range=(0, 2000), precision=1,
           description="Total carbohydrate."),
    Column("total_fat_g", "nutrition", "number", "g", "meals", WAKING_DAY,
           NEUTRAL, tier=1, range=(0, 1000), precision=1,
           description="Total fat."),
    _nutrient("fiber_g", "g", "dietary fibre", tier=1),
    _nutrient("sugar_g", "g", "sugars", tier=1),
    _nutrient("saturated_fat_g", "g", "saturated fat"),
    _nutrient("sodium_mg", "mg", "sodium", tier=1),
    _nutrient("potassium_mg", "mg", "potassium"),
    _nutrient("calcium_mg", "mg", "calcium"),
    _nutrient("iron_mg", "mg", "iron"),
    _nutrient("magnesium_mg", "mg", "magnesium"),
    _nutrient("zinc_mg", "mg", "zinc"),
    _nutrient("vitamin_c_mg", "mg", "vitamin C"),
    _nutrient("vitamin_d_ug", "ug", "vitamin D"),
    _nutrient("vitamin_b12_ug", "ug", "vitamin B12"),
    _nutrient("vitamin_a_ug", "ug", "vitamin A"),
    _nutrient("folate_ug", "ug", "folate"),
    _nutrient("omega3_g", "g", "omega-3 fatty acids"),

    # -- body composition (OCR'd from the scale app screenshot) ----------------
    Column("weight_kg", "body", "number", "kg", "scale", MORNING_OF, NEUTRAL,
           tier=1, range=(20, 300), precision=2,
           description="Body weight. Direction is deliberately neutral — whether up "
                       "is good depends on a goal this data doesn't contain. Day-to-day "
                       "swings are mostly water; read the trend, not the delta."),
    Column("bmi", "body", "number", "", "scale", MORNING_OF, NEUTRAL, tier=2,
           range=(8, 70), precision=1,
           description="Body mass index as computed by the scale. Low information "
                       "given body fat and lean mass are measured directly."),
    Column("body_fat_pct", "body", "number", "%", "scale", MORNING_OF, DOWN_GOOD,
           tier=1, range=(2, 70), precision=1,
           description="Body fat percentage from bioimpedance. Hydration-sensitive, "
                       "so trust the multi-day trend over any single reading."),
    Column("subcutaneous_fat_pct", "body", "number", "%", "scale", MORNING_OF,
           DOWN_GOOD, tier=2, range=(1, 60), precision=1,
           description="Fat stored under the skin, as a percentage."),
    Column("visceral_fat", "body", "number", "index", "scale", MORNING_OF,
           DOWN_GOOD, tier=1, range=(1, 60), precision=1,
           description="Visceral fat index (a unitless scale value, not a percentage "
                       "or a mass). Fat around the organs; the most metabolically "
                       "relevant fat measure here."),
    Column("body_water_pct", "body", "number", "%", "scale", MORNING_OF, NEUTRAL,
           tier=2, range=(20, 85), precision=1,
           description="Total body water. Explains much of the day-to-day noise in "
                       "weight and body-fat readings."),
    Column("muscle_mass_kg", "body", "number", "kg", "scale", MORNING_OF, UP_GOOD,
           tier=1, range=(10, 150), precision=1,
           description="Skeletal muscle mass. Should hold or rise while weight falls "
                       "— that is what successful recomposition looks like."),
    Column("bone_mass_kg", "body", "number", "kg", "scale", MORNING_OF, NEUTRAL,
           tier=2, range=(0.5, 10), precision=2,
           description="Bone mass. Changes very slowly; effectively a constant."),
    Column("bmr_kcal", "body", "number", "kcal", "scale", MORNING_OF, NEUTRAL,
           tier=2, range=(600, 5000), precision=0,
           description="Basal metabolic rate as estimated by the scale from lean "
                       "mass. Compare against Fitbit's measured total_cals_out."),
    Column("metabolic_age", "body", "number", "years", "scale", MORNING_OF,
           DOWN_GOOD, tier=2, range=(5, 120), precision=0,
           description="The scale's proprietary 'metabolic age' estimate. Vendor "
                       "marketing metric — low scientific value; kept for completeness."),
    Column("lean_mass_kg", "body", "number", "kg", "derived", MORNING_OF, UP_GOOD,
           tier=1, range=(10, 200), precision=2,
           description="weight_kg x (1 - body_fat_pct/100). Everything that isn't "
                       "fat. The number that should stay flat while you cut."),
    Column("body_measured_at", "body", "datetime", "", "scale", MORNING_OF,
           NEUTRAL, tier=2,
           description="Local timestamp of the weigh-in, read off the app screen. "
                       "The hour is signal: a fasted 07:00 reading and a 21:00 one "
                       "are not comparable."),

    # -- bookkeeping -----------------------------------------------------------
    Column("updated_at", "meta", "datetime", "", "system", NO_WINDOW, NEUTRAL,
           tier=2,
           description="UTC timestamp of the last write to this row."),
]


# -- lookups -------------------------------------------------------------------
BY_NAME: Dict[str, Column] = {c.name: c for c in DAILY_COLUMNS}


def daily_headers() -> List[str]:
    """The sheet's header row, in column order."""
    return [c.name for c in DAILY_COLUMNS]


def columns_in(block: str) -> List[Column]:
    return [c for c in DAILY_COLUMNS if c.block == block]


def names_in(block: str) -> List[str]:
    return [c.name for c in columns_in(block)]


def names_from(source: str) -> List[str]:
    return [c.name for c in DAILY_COLUMNS if c.source == source]


def ocr_ranges() -> Dict[str, Tuple[float, float]]:
    """Plausibility bands for the metrics the ingest service OCRs off the scale
    screenshot. Reading digits off a phone screen is where a model is most
    confidently wrong (a dropped decimal turns 70.05 kg into 7005), so anything
    outside its band is dropped rather than written. Derived here so the ingest
    service and the sheet can never disagree about what a body may be."""
    return {c.name: c.range for c in DAILY_COLUMNS
            if c.block == "body" and c.source == "scale" and c.range is not None}


def causal_inputs() -> List[Column]:
    """Columns describing what I did during day N."""
    return [c for c in DAILY_COLUMNS if c.role == "input"]


def causal_outcomes() -> List[Column]:
    """Columns describing what my body did — caused by the PREVIOUS day's inputs."""
    return [c for c in DAILY_COLUMNS if c.role == "outcome"]


def numeric_columns() -> List[Column]:
    return [c for c in DAILY_COLUMNS if c.dtype in ("number", "integer")]


def validate() -> None:
    """Fail loudly on a malformed registry — it generates the sheet layout, the
    API and the type exports, so a typo here corrupts all of them at once."""
    names = daily_headers()
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"duplicate column name(s): {dupes}")
    if names[0] != "date":
        raise ValueError("`date` must be the first column (it keys the upsert)")
    if names[-1] != "updated_at":
        raise ValueError("`updated_at` must stay last (bookkeeping)")
    for c in DAILY_COLUMNS:
        if c.block not in BLOCKS:
            raise ValueError(f"{c.name}: unknown block {c.block!r}")
        if c.source not in SOURCES:
            raise ValueError(f"{c.name}: unknown source {c.source!r}")
        if c.causal not in CAUSAL_LABELS:
            raise ValueError(f"{c.name}: unknown causal window {c.causal!r}")
        if c.direction not in (UP_GOOD, DOWN_GOOD, NEUTRAL):
            raise ValueError(f"{c.name}: unknown direction {c.direction!r}")
        if c.range and c.range[0] >= c.range[1]:
            raise ValueError(f"{c.name}: empty range {c.range}")
        if not c.description.strip():
            raise ValueError(f"{c.name}: needs a description — an AI reads this")
    # Blocks must be contiguous, or the sheet's column groups would interleave.
    seen: List[str] = []
    for c in DAILY_COLUMNS:
        if not seen or seen[-1] != c.block:
            if c.block in seen:
                raise ValueError(f"block {c.block!r} is not contiguous")
            seen.append(c.block)


validate()
