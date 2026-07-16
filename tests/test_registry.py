"""The registry generates the sheet layout, the API and the type exports, so a
mistake here corrupts all of them at once. These tests are the guard rail."""
import pytest

from schema.registry import (
    BLOCKS, CAUSAL_INPUT, CAUSAL_OUTCOME, DAILY_COLUMNS, MORNING_OF, NIGHT_ENDING,
    WAKING_DAY, causal_inputs, causal_outcomes, daily_headers, names_in,
    numeric_columns, ocr_ranges, validate,
)


def test_registry_is_valid():
    validate()  # also runs at import; explicit here so a break names this test


def test_every_column_is_fully_described():
    for c in DAILY_COLUMNS:
        assert c.description.strip(), f"{c.name} has no description"
        assert c.block in BLOCKS
        assert c.dtype in ("date", "datetime", "time", "number", "integer",
                           "boolean", "string")
        # A number without a unit is a trap for whoever reads it next.
        if c.dtype in ("number", "integer"):
            assert c.unit or c.name in ("bmi", "hrv_entropy"), \
                f"{c.name} is numeric but declares no unit"


def test_date_keys_the_table_and_updated_at_stays_last():
    headers = daily_headers()
    assert headers[0] == "date"
    assert headers[-1] == "updated_at"
    assert len(headers) == len(set(headers))


def test_blocks_are_contiguous():
    # Column groups in the sheet span a block; interleaved blocks would make the
    # collapsible groups overlap and the layout meaningless.
    seen = []
    for c in DAILY_COLUMNS:
        if not seen or seen[-1] != c.block:
            assert c.block not in seen, f"block {c.block} is split"
            seen.append(c.block)


# -- the causal contract -------------------------------------------------------
def test_inputs_and_outcomes_partition_every_measurement():
    inputs, outcomes = set(c.name for c in causal_inputs()), \
                       set(c.name for c in causal_outcomes())
    assert not inputs & outcomes                       # nothing is both
    unclassified = {c.name for c in DAILY_COLUMNS} - inputs - outcomes
    assert unclassified == {"date", "updated_at"}      # only the non-measurements


def test_the_things_i_do_are_inputs_and_my_body_is_an_outcome():
    # This is the whole point of the causal field: food/activity are things I did
    # during day N; sleep/recovery/weight are what my body did afterwards, and are
    # therefore caused by the PREVIOUS day's inputs.
    for name in ("total_cals_in", "total_protein_g", "steps", "total_cals_out"):
        assert name in {c.name for c in causal_inputs()}, name
    for name in ("sleep_mins", "sleep_efficiency_pct", "hrv_ms", "resting_hr_bpm",
                 "weight_kg", "body_fat_pct"):
        assert name in {c.name for c in causal_outcomes()}, name


def test_causal_windows_match_the_pipeline():
    from schema.registry import BY_NAME
    assert BY_NAME["total_cals_in"].causal == WAKING_DAY      # 05:00 -> 05:00
    assert BY_NAME["sleep_mins"].causal == NIGHT_ENDING       # night before
    assert BY_NAME["hrv_ms"].causal == NIGHT_ENDING
    assert BY_NAME["weight_kg"].causal == MORNING_OF          # fasted, that morning
    assert BY_NAME["steps"].causal == "calendar_day"
    # A nap is the exception in the sleep block: it happened DURING the day.
    assert BY_NAME["nap_mins"].causal == "day_of"


# -- what other modules depend on ----------------------------------------------
def test_ocr_ranges_cover_exactly_the_scale_metrics():
    ranges = ocr_ranges()
    assert len(ranges) == 10                    # the ten the scale computes
    assert "weight_kg" in ranges and ranges["weight_kg"] == (20, 300)
    # derived and stamped columns are NOT OCR'd and must not be validated as if
    assert "lean_mass_kg" not in ranges
    assert "body_measured_at" not in ranges
    for name, (lo, hi) in ranges.items():
        assert lo < hi, name


def test_sheets_and_ingest_read_the_same_schema():
    # The duplication this replaced: BODY_METRICS used to be typed out in both
    # src/sheets.py and ingest/main.py, held together only by a test.
    from src.sheets import BODY_METRICS, DAILY_HEADERS
    assert DAILY_HEADERS == daily_headers()
    assert BODY_METRICS == list(ocr_ranges())


def test_there_is_no_sleep_score():
    # Proprietary to Fitbit, absent from the Google Health API. A column for it
    # could only ever be blank; sleep_efficiency_pct is the honest stand-in.
    names = daily_headers()
    assert "sleep_score" not in names
    assert "sleep_efficiency_pct" in names


def test_energy_balance_is_present_and_derived():
    from schema.registry import BY_NAME
    col = BY_NAME["energy_balance_kcal"]
    assert col.source == "derived"
    assert col.tier == 1
    assert "total_cals_in" in col.description and "total_cals_out" in col.description


def test_numeric_columns_excludes_dates_and_flags():
    numeric = {c.name for c in numeric_columns()}
    assert "hrv_ms" in numeric
    assert "date" not in numeric
    assert "bowel_movement" not in numeric        # boolean, not a number
    assert "sleep_start" not in numeric           # a clock time
