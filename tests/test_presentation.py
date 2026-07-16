"""Tests for the sheet's presentation layer.

79 columns is an ordinary width for a fact table and an awful one for a browser.
The fix is presentational — folding, freezing, formatting — not structural.
"""
from schema.registry import DAILY_COLUMNS, columns_in, daily_headers
from src.presentation import (
    SCHEMA_HEADERS, block_groups, clear_group_requests, collapse_requests,
    format_requests, header_note_requests, schema_rows,
)


# -- the group-merge trap ------------------------------------------------------
def test_groups_never_touch_each_other():
    # THE bug this guards: Sheets merges adjacent column groups at the same depth,
    # so grouping B:C next to D:O silently becomes one group over B:O — which
    # collapses everything or nothing. Leaving a gap keeps them separate.
    groups = sorted(block_groups(), key=lambda g: g["start"])
    for a, b in zip(groups, groups[1:]):
        assert a["end"] < b["start"], (
            f"{a['block']} ends at {a['end']} and {b['block']} starts at "
            f"{b['start']} — adjacent groups merge into one")


def test_each_group_skips_its_blocks_first_column():
    # That column is the block's anchor: it stays visible when the block is folded.
    index = 0
    starts = {}
    for c in DAILY_COLUMNS:
        starts.setdefault(c.block, index)
        index += 1
    for g in block_groups():
        assert g["start"] == starts[g["block"]] + 1


def test_a_collapsed_sheet_still_shows_the_headlines():
    # What you actually see with every block folded. Each must be a tier-1 metric,
    # or the collapsed view is useless.
    headers = daily_headers()
    grouped = {i for g in block_groups() for i in range(g["start"], g["end"])}
    visible = [headers[i] for i in range(len(headers)) if i not in grouped]
    assert visible == [
        "date", "bowel_movement", "sleep_mins",
        "resting_hr_bpm", "total_cals_out", "energy_balance_kcal", "weight_kg",
        "updated_at",
    ]
    from schema.registry import BY_NAME
    for name in visible:
        if name not in ("date", "updated_at", "bowel_movement"):
            assert BY_NAME[name].tier == 1, f"{name} anchors a block but isn't tier 1"


def test_small_blocks_are_not_grouped():
    # self_report is 2 columns; grouping it would hide exactly one.
    assert "self_report" not in {g["block"] for g in block_groups()}
    assert {g["block"] for g in block_groups()} == {
        "sleep", "recovery", "activity", "nutrition", "body"}


def test_groups_stay_inside_their_block():
    index = 0
    bounds = {}
    for block in [c.block for c in DAILY_COLUMNS]:
        pass
    for g in block_groups():
        span = len(columns_in(g["block"]))
        assert g["end"] - g["start"] == span - 1


# -- requests ------------------------------------------------------------------
def test_format_requests_freeze_the_date_and_header():
    reqs = format_requests(7)
    frozen = [r for r in reqs if "updateSheetProperties" in r][0]
    grid = frozen["updateSheetProperties"]["properties"]["gridProperties"]
    assert grid["frozenRowCount"] == 1
    assert grid["frozenColumnCount"] == 1        # `date` stays put while scrolling


def test_every_group_is_collapsed_by_default():
    collapse = collapse_requests(7)
    assert len(collapse) == len(block_groups())
    assert all(r["updateDimensionGroup"]["dimensionGroup"]["collapsed"]
               for r in collapse)


def test_clear_requests_delete_whatever_is_there():
    existing = [{"range": {"startIndex": 1, "endIndex": 78}, "depth": 1}]
    reqs = clear_group_requests(7, existing)
    assert reqs[0]["deleteDimensionGroup"]["range"]["startIndex"] == 1
    assert reqs[0]["deleteDimensionGroup"]["range"]["endIndex"] == 78
    assert clear_group_requests(7, []) == []


def test_numeric_columns_get_a_format_matching_their_unit():
    reqs = [r for r in format_requests(7) if "repeatCell" in r
            and "numberFormat" in str(r)]
    # every kg/%/kcal column should be formatted; none of the dates or times
    assert len(reqs) > 40
    headers = daily_headers()
    formatted = {headers[r["repeatCell"]["range"]["startColumnIndex"]] for r in reqs}
    assert "weight_kg" in formatted and "steps" in formatted
    assert "date" not in formatted and "sleep_start" not in formatted


def test_header_notes_carry_the_meaning_and_the_causal_window():
    notes = header_note_requests(7)
    assert len(notes) == len(DAILY_COLUMNS)
    first = notes[0]["updateCells"]["rows"][0]["values"][0]["note"]
    assert "unit:" in first and "measures:" in first
    # spot-check the one that matters most
    headers = daily_headers()
    sleep_note = notes[headers.index("sleep_mins")]["updateCells"]["rows"][0]["values"][0]["note"]
    assert "night that ended" in sleep_note


# -- the schema tab ------------------------------------------------------------
def test_schema_tab_documents_every_column():
    rows = schema_rows()
    assert len(rows) == len(DAILY_COLUMNS)
    assert len(rows[0]) == len(SCHEMA_HEADERS)
    by_name = {r[0]: dict(zip(SCHEMA_HEADERS, r)) for r in rows}
    sleep = by_name["sleep_mins"]
    assert sleep["unit"] == "min"
    assert sleep["direction"] == "up_good"
    assert "night that ended" in sleep["measures_when"]
    assert sleep["description"]
    # ranges are published so a reader knows what's plausible
    assert by_name["weight_kg"]["min"] == 20 and by_name["weight_kg"]["max"] == 300
