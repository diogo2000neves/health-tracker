"""Google Sheets access for the per-day health model.

* ``daily_summary`` — one row per calendar day holding the 24h "readiness ->
  output" vector (sleep/recovery stamped on the wake day) plus the day's nutrition
  roll-up and full body composition. Rows are *merge-upserted* on ``date`` so
  independent sources (the scale screenshot, the meals roll-up, the /feel endpoint
  and — later — Fitbit biometrics) each fill their own columns without clobbering
  the rest.

* ``meals`` — written by the ingest service; read here only as the granular
  nutrition source for the daily roll-up, and otherwise left untouched.

Day grain: every ``date`` in the model is the **local civil day** (the day the
user experienced), never the UTC day.

All access uses the Cloud Run runtime service account (ADC) scoped to
spreadsheets; the target Sheet must be shared (Editor) with that account.
Numeric cells are always read with ``UNFORMATTED_VALUE`` — the sheet's European
locale renders decimals with commas, and formatted reads would silently break
``float()`` parsing downstream.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from googleapiclient.discovery import build

from schema.registry import daily_headers, names_in, ocr_ranges

DAILY_TAB = "daily_summary"
MEALS_TAB = "meals"
DASHBOARD_TAB = "dashboard"
INSIGHTS_TAB = "insights"
SCHEMA_TAB = "schema"
ANALYSIS_TAB = "analysis"
BASELINES_TAB = "baselines"

# The schema now lives in ONE place: schema/registry.py, which declares every
# column's unit, source, causal window, direction, plausible range and description.
# These names are re-exported for readability at the call sites; nothing here
# defines the schema any more. Add or change a column in the registry, then run
# `python -m src.maintenance` to migrate the sheet to match.
DAILY_HEADERS: List[str] = daily_headers()

# The ten metrics OCR'd from the scale screenshot. The ingest service reads the
# same list (with its plausibility bands) straight from the registry, so the two
# can no longer drift apart the way the old hand-mirrored copies could.
BODY_METRICS: List[str] = [n for n in ocr_ranges()]

# Micronutrients that get a daily total column (`total_<key>`). The full ~36
# nutrient set lives per-ingredient in the meals `items` JSON; this is the
# high-value subset that additionally rolls up.
TIER1_NUTRIENTS: List[str] = [
    n[len("total_"):] for n in names_in("nutrition")
    if n.startswith("total_") and n not in
    ("total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g")
]

# The dashboard tab's stat block, in order from cell B3: (label, source column,
# how to reduce it). maintenance.py writes the labels in column A and run_daily.py
# writes the values in column B — they read this one list so the two can never
# drift out of alignment.
#   latest — the most recent non-empty value in that column
#   avg7   — mean over the last 7 days that have nutrition logged
#   avgd7  — mean over the last 7 *calendar* days that have this column filled
#            (biometrics arrive every day the tracker is worn, independently of
#            whether meals were logged, so they must not use the nutrition window)
#   days7  — how many nutrition-logged days exist
#   count7 — how many of the last 7 calendar days have TRUE in that column
#   now    — the refresh timestamp
DASHBOARD_STATS: List[tuple] = [
    ("Latest weight (kg)", "weight_kg", "latest"),
    ("Latest BMI", "bmi", "latest"),
    ("Latest body fat (%)", "body_fat_pct", "latest"),
    ("Latest subcutaneous fat (%)", "subcutaneous_fat_pct", "latest"),
    ("Latest visceral fat", "visceral_fat", "latest"),
    ("Latest body water (%)", "body_water_pct", "latest"),
    ("Latest muscle mass (kg)", "muscle_mass_kg", "latest"),
    ("Latest bone mass (kg)", "bone_mass_kg", "latest"),
    ("Latest lean mass (kg)", "lean_mass_kg", "latest"),
    ("Latest BMR (kcal)", "bmr_kcal", "latest"),
    ("Latest metabolic age", "metabolic_age", "latest"),
    ("Last weigh-in", "body_measured_at", "latest"),
    ("Avg kcal in (last 7 logged days)", "total_cals_in", "avg7"),
    ("Avg protein g (7d)", "total_protein_g", "avg7"),
    ("Avg carbs g (7d)", "total_carbs_g", "avg7"),
    ("Avg fat g (7d)", "total_fat_g", "avg7"),
    ("Nutrition days in window", "", "days7"),
    ("Bowel movements (last 7 days)", "bowel_movement", "count7"),
    # Fitbit Air. `latest` skips blanks, so these show the last night actually
    # recorded rather than going empty on a day the tracker wasn't worn.
    ("Last night asleep (mins)", "sleep_mins", "latest"),
    ("Last night efficiency (%)", "sleep_efficiency_pct", "latest"),
    ("Last night deep (mins)", "sleep_deep_mins", "latest"),
    ("Last night REM (mins)", "sleep_rem_mins", "latest"),
    ("Latest resting HR (bpm)", "resting_hr_bpm", "latest"),
    ("Latest HRV (ms)", "hrv_ms", "latest"),
    ("Latest SpO2 (%)", "spo2_pct", "latest"),
    ("Latest skin temp dev (C)", "skin_temp_dev", "latest"),
    ("Avg steps (7d)", "steps", "avgd7"),
    ("Avg calories out (7d)", "total_cals_out", "avgd7"),
    ("Stats updated (UTC)", "", "now"),
]
DASHBOARD_FIRST_ROW = 3  # the stat values start at B3, under the title

# Merge-upsert keys this column; everything else in a row is left to its owner.
_DAILY_KEY = "date"


def col_letter(index: int) -> str:
    """0-based column index -> A1 letter, e.g. 0->'A', 26->'AA', 51->'AZ'."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# Read ranges are derived from the schema, never hard-coded: daily_summary has
# already outgrown A:Z once (silently truncating the header, so columns past the
# cut looked "missing" and their writes went nowhere) and is now ~84 wide. The
# headroom lets a few columns be added before this needs a thought.
READ_LAST_COL = col_letter(len(DAILY_HEADERS) + 40)


class SheetClient:
    """Thin, schema-aware wrapper over the Sheets v4 API."""

    def __init__(self, creds, spreadsheet_id: str):
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sid = spreadsheet_id
        self._titles: Optional[set] = None

    # -- structure ------------------------------------------------------
    def tab_titles(self, refresh: bool = False) -> set:
        if self._titles is None or refresh:
            meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
            self._titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        return self._titles

    def ensure_tab(self, title: str, headers: Sequence[str]) -> None:
        """Create the tab if missing and (re)write its header row if it drifts."""
        if title not in self.tab_titles():
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ).execute()
            self._titles = None
        rng = f"{title}!A1:{col_letter(len(headers) - 1)}1"
        current = (
            self.svc.spreadsheets().values()
            .get(spreadsheetId=self.sid, range=rng)
            .execute().get("values", [[]])
        )
        if not current or current[0] != list(headers):
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{title}!A1",
                valueInputOption="RAW", body={"values": [list(headers)]},
            ).execute()

    def sheet_id(self, tab: str) -> Optional[int]:
        """The tab's numeric sheetId (needed to sort it), or None if absent."""
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["title"] == tab:
                return sheet["properties"]["sheetId"]
        return None

    def sort_by_date(self, tab: str) -> None:
        """Order a tab by its first column (the date/datetime key).

        Rows are appended in arrival order, not date order — and a backfilled scale
        screenshot arrives *after* the days that follow it. Left unsorted, the
        dashboard's line chart plots that day out of sequence and the trend is a
        lie. ISO dates sort lexicographically, so a plain ascending sort is
        chronological."""
        tab_id = self.sheet_id(tab)
        if tab_id is None:
            return
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,
            body={"requests": [{"sortRange": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": 0},
                "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
            }}]},
        ).execute()

    def header(self, tab: str) -> List[str]:
        """The tab's header row as written in the sheet ([] if the tab is absent)."""
        if tab not in self.tab_titles():
            return []
        values = (
            self.svc.spreadsheets().values()
            .get(spreadsheetId=self.sid, range=f"{tab}!1:1")
            .execute().get("values", [[]])
        )
        return values[0] if values else []

    # -- reads ----------------------------------------------------------
    def read_rows(self, tab: str) -> List[Dict[str, Any]]:
        """Return data rows as header-keyed dicts ([] if the tab is absent)."""
        if tab not in self.tab_titles():
            return []
        values = (
            self.svc.spreadsheets().values()
            .get(
                spreadsheetId=self.sid,
                range=f"{tab}!A1:{READ_LAST_COL}",
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute().get("values", [])
        )
        if len(values) < 2:
            return []
        headers = values[0]
        return [dict(zip(headers, row)) for row in values[1:]]

    # -- writes ----------------------------------------------------------
    def write_values(self, tab: str, a1: str, values: List[List[Any]],
                     user_entered: bool = False) -> None:
        """Write a block of values anchored at `a1` (e.g. stat cells, formulas)."""
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid, range=f"{tab}!{a1}",
            valueInputOption="USER_ENTERED" if user_entered else "RAW",
            body={"values": values},
        ).execute()

    def replace_tab(self, tab: str, values: List[List[Any]]) -> None:
        """Overwrite a tab wholesale (creating it if absent).

        Only for **derived** tabs — `analysis`, `baselines` — which are pure
        functions of daily_summary and rebuilt every run. Never point this at a
        tab that holds observations."""
        if tab not in self.tab_titles():
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
            self._titles = None
        self.svc.spreadsheets().values().clear(
            spreadsheetId=self.sid, range=tab).execute()
        if values:
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{tab}!A1",
                valueInputOption="RAW", body={"values": values},
            ).execute()

    def append_row(self, tab: str, row: List[Any]) -> None:
        self.svc.spreadsheets().values().append(
            spreadsheetId=self.sid, range=f"{tab}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    # -- daily_summary: merge-upsert on date ----------------------------
    def upsert_daily(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Merge rows into daily_summary keyed on `date`.

        Only fields present (and non-None) in a row overwrite existing cells, so
        a nutrition-only or body-only update never blanks columns owned by
        another source. New dates are appended.
        """
        width = len(DAILY_HEADERS)
        grid = (
            self.svc.spreadsheets().values()
            .get(
                spreadsheetId=self.sid,
                range=f"{DAILY_TAB}!A2:{col_letter(width - 1)}",
                # UNFORMATTED so existing numbers are read (and rewritten) as
                # numbers rather than locale strings that RAW would store as text.
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute().get("values", [])
        )
        date_to_index = {str(r[0]): i for i, r in enumerate(grid) if r}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        updates: List[Dict[str, Any]] = []
        appends: List[List[Any]] = []
        for row in rows:
            incoming = {**row, "updated_at": now}
            key = row[_DAILY_KEY]
            if key in date_to_index:
                existing = grid[date_to_index[key]]
                existing += [None] * (width - len(existing))  # pad ragged row
                values = [
                    incoming[h] if incoming.get(h) is not None else existing[j]
                    for j, h in enumerate(DAILY_HEADERS)
                ]
                rownum = date_to_index[key] + 2
                updates.append({
                    "range": f"{DAILY_TAB}!A{rownum}:{col_letter(width - 1)}{rownum}",
                    "values": [values],
                })
            else:
                appends.append([incoming.get(h) for h in DAILY_HEADERS])

        if updates:
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
        if appends:
            self.svc.spreadsheets().values().append(
                spreadsheetId=self.sid, range=f"{DAILY_TAB}!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": appends},
            ).execute()
        return {"updated": len(updates), "appended": len(appends)}
