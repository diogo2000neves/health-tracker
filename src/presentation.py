"""Make `daily_summary` pleasant to look at, and self-describing.

79 columns is a perfectly ordinary width for a fact table and a genuinely awful
width for a browser window. That is a *presentation* problem, not a modelling one —
splitting the table to make it fit would buy a join and lose the ability to read a
day across. So the fix stays here: fold the blocks away, freeze the date, format
the units, and publish a dictionary of what every column means.

Everything is generated from `schema/registry.py`, so it can never describe a
column that doesn't exist or miss one that does.
"""
from __future__ import annotations

from typing import Any, Dict, List

from schema.registry import (
    BLOCK_LABELS, CAUSAL_LABELS, DAILY_COLUMNS, columns_in,
)

SCHEMA_HEADERS = [
    "column", "block", "type", "unit", "source", "measures_when", "direction",
    "tier", "min", "max", "description",
]

# Number formats per unit. Sheets renders these locally (the sheet is a European
# locale), which is why we set patterns rather than writing formatted strings.
_FORMATS: Dict[str, str] = {
    "kg": "0.00",
    "%": "0.0",
    "ms": "0.00",
    "C": "0.00",
    "km": "0.00",
    "min": "0",
    "kcal": "0",
    "bpm": "0",
    "count": "#,##0",
    "g": "0.0",
    "mg": "0.0",
    "ug": "0.0",
    "breaths/min": "0.0",
    "years": "0",
    "index": "0.0",
    "1-10": "0.0",
}


def schema_rows() -> List[List[Any]]:
    """The `schema` tab: one row per column, the data dictionary.

    This is the semantic layer. Without it an agent reading the sheet has to guess
    what `hrv_entropy` is, what unit `skin_temp_dev` uses, and whether a high
    `resting_hr_bpm` is good news — and it will guess wrong. `measures_when` is the
    load-bearing one: it is what tells a reader that sleep on a row happened the
    night *before* that date.
    """
    rows: List[List[Any]] = []
    for c in DAILY_COLUMNS:
        lo, hi = (c.range if c.range else ("", ""))
        rows.append([
            c.name, c.block, c.dtype, c.unit, c.source,
            CAUSAL_LABELS[c.causal], c.direction, c.tier, lo, hi, c.description,
        ])
    return rows


def schema_legend() -> List[str]:
    return [
        "DATA DICTIONARY for daily_summary — generated from schema/registry.py, "
        "do not edit by hand. `measures_when` is the important column: a row is an "
        "observation of a date, NOT a causal unit. Sleep and recovery on row N "
        "happened the night BEFORE N; food and activity happened during N, after "
        "both. To correlate cause with effect, pair each day's inputs with the "
        "next day's outcomes. `baselines` says what is normal for this person — "
        "a raw value alone is not interpretable."
    ]


def _grid_range(sheet_id: int, start: int, end: int) -> Dict[str, Any]:
    return {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": start, "endIndex": end}


def block_groups() -> List[Dict[str, Any]]:
    """The collapsible column range for each block: [name, start, end).

    Each group deliberately **skips its block's first column**, for two reasons:

    1. Sheets *merges adjacent groups at the same depth*. Grouping B:C and D:O
       side by side silently produces one group spanning B:O — collapse-all or
       nothing, which is useless. Leaving one ungrouped column between them keeps
       them distinct.
    2. That skipped column becomes the block's anchor: it stays visible when the
       block is folded, so a fully collapsed sheet still reads
       `date | feel | sleep_mins | resting_hr | steps | energy_balance | weight`.
       The registry orders each block headline-first for exactly this.

    Blocks of fewer than 3 columns aren't worth a group (you'd hide one column).
    """
    out: List[Dict[str, Any]] = []
    index = 0
    for block in _block_order():
        span = len(columns_in(block))
        if block not in ("key", "meta") and span >= 3:
            out.append({"block": block, "start": index + 1, "end": index + span})
        index += span
    return out


def format_requests(daily_id: int) -> List[Dict[str, Any]]:
    """batchUpdate requests that make the daily_summary tab readable.

    * freeze the header row and the date column, so both stay put while scrolling
      across 79 columns;
    * one collapsible group per block — this is what actually solves "the table is
      unusable": fold away nutrition when you're looking at sleep;
    * per-unit number formats, so kg reads as 70.05 and steps as 4,151;
    * a bold header and banded rows.
    """
    requests: List[Dict[str, Any]] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": daily_id,
                           "gridProperties": {"frozenRowCount": 1,
                                              "frozenColumnCount": 1}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }},
        {"repeatCell": {
            "range": {"sheetId": daily_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "verticalAlignment": "BOTTOM",
                "wrapStrategy": "CLIP",
            }},
            "fields": "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy)",
        }},
    ]

    for group in block_groups():
        requests.append({"addDimensionGroup": {
            "range": _grid_range(daily_id, group["start"], group["end"])}})

    # Number formats, driven by each column's declared unit.
    for i, col in enumerate(DAILY_COLUMNS):
        pattern = _FORMATS.get(col.unit)
        if col.dtype in ("number", "integer") and pattern:
            requests.append({"repeatCell": {
                "range": {"sheetId": daily_id, "startRowIndex": 1,
                          "startColumnIndex": i, "endColumnIndex": i + 1},
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }})
    return requests


def clear_group_requests(daily_id: int,
                         existing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop the groups already on the sheet so they can be rebuilt cleanly.

    Needed because groups are positional: after a schema reorder the old ranges
    span the wrong columns, and because Sheets silently merges adjacent groups,
    a stale run can leave one giant group that no new request will match."""
    return [{"deleteDimensionGroup": {"range": _grid_range(
        daily_id, g["range"]["startIndex"], g["range"]["endIndex"])}}
        for g in existing]


def collapse_requests(daily_id: int) -> List[Dict[str, Any]]:
    """Collapse every block by default: the tab then opens on ~7 headline columns
    plus a `+` per block, instead of a wall of 79. Applied in a second pass because
    a group must exist before it can be collapsed."""
    return [{"updateDimensionGroup": {
        "dimensionGroup": {
            "range": _grid_range(daily_id, g["start"], g["end"]),
            "depth": 1, "collapsed": True,
        },
        "fields": "collapsed",
    }} for g in block_groups()]


def _block_order() -> List[str]:
    """Blocks in the order they appear in the schema (registry.validate() has
    already guaranteed each one is contiguous)."""
    seen: List[str] = []
    for c in DAILY_COLUMNS:
        if not seen or seen[-1] != c.block:
            seen.append(c.block)
    return seen


def header_note_requests(daily_id: int) -> List[Dict[str, Any]]:
    """Attach each column's description as a cell note on its header.

    Hovering a header in the sheet then tells you what it means, its unit and when
    it was measured — the dictionary, available exactly where the question occurs."""
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(DAILY_COLUMNS):
        note = (f"{c.description}\n\n"
                f"unit: {c.unit or 'n/a'} | source: {c.source} | "
                f"direction: {c.direction}\n"
                f"measures: {CAUSAL_LABELS[c.causal]}")
        out.append({"updateCells": {
            "range": {"sheetId": daily_id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": i, "endColumnIndex": i + 1},
            "rows": [{"values": [{"note": note}]}],
            "fields": "note",
        }})
    return out
