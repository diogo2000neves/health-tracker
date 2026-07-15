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

DAILY_TAB = "daily_summary"
MEALS_TAB = "meals"
DASHBOARD_TAB = "dashboard"
INSIGHTS_TAB = "insights"

# Tier-1 micronutrients that also roll up into daily_summary as `total_<key>`
# columns — the high-value, more-reliably-estimated ones. The full nutrient set
# is stored per-ingredient in the meals `items` JSON (owned by ingest/main.py);
# these are just the subset that additionally gets daily totals. Keys carry their
# unit suffix (_g / _mg / _ug) so they map cleanly to a future relational schema.
TIER1_NUTRIENTS: List[str] = [
    "fiber_g", "sugar_g", "saturated_fat_g", "sodium_mg", "potassium_mg",
    "calcium_mg", "iron_mg", "magnesium_mg", "zinc_mg",
    "vitamin_c_mg", "vitamin_d_ug", "vitamin_b12_ug", "vitamin_a_ug",
    "folate_ug", "omega3_g",
]

# Body composition, read straight off the smart scale app's result screen (the
# user screenshots it and the ingest service OCRs every value with Gemini). These
# are the ten metrics the scale actually computes from bioimpedance — the Google
# Health API only ever exposed the first three, which is why we stopped using it.
# Order = the order they appear on the app screen.
#
# Mirrored in ingest/main.py BODY_METRICS, which owns the plausibility ranges and
# is a separate container image that can't import this module. `tests/test_sheets`
# asserts the two stay in step.
BODY_METRICS: List[str] = [
    "weight_kg", "bmi", "body_fat_pct", "subcutaneous_fat_pct", "visceral_fat",
    "body_water_pct", "muscle_mass_kg", "bone_mass_kg", "bmr_kcal",
    "metabolic_age",
]

# Parent schema. Readiness block first (blueprint order), then the nutrition
# roll-up (macros + Tier-1 micronutrients), then activity and body composition;
# `updated_at` stays last as bookkeeping. `lean_mass_kg` = weight_kg x
# (1 - body_fat_pct/100) — stored, not just derived, so the sheet stays
# self-contained for AI analysis. `body_measured_at` is the reading's own
# timestamp as printed in the app (a 07:00 fasted weigh-in reads differently from
# a 21:00 one, so the hour is signal, not bookkeeping). `bowel_movement` is a
# TRUE/blank flag that sits beside `subjective_feel` — both are things the user
# self-reports about a day rather than sensor readings; it's set from a plain text
# note ("fiz cocó") via the ingest service. Schema changes must go through
# src/maintenance.py so existing rows are realigned in place, never clobbered.
DAILY_HEADERS: List[str] = [
    "date",
    "sleep_score", "hrv_ms", "spo2_pct", "skin_temp_dev",
    "subjective_feel", "bowel_movement",
    "total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g",
    *[f"total_{n}" for n in TIER1_NUTRIENTS],
    "total_active_mins", "steps",
    *BODY_METRICS, "lean_mass_kg", "body_measured_at",
    "updated_at",
]

# The dashboard tab's stat block, in order from cell B3: (label, source column,
# how to reduce it). maintenance.py writes the labels in column A and run_daily.py
# writes the values in column B — they read this one list so the two can never
# drift out of alignment.
#   latest — the most recent non-empty value in that column
#   avg7   — mean over the last 7 days that have nutrition logged
#   days7  — how many such days exist
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
    ("Avg kcal (last 7 logged days)", "total_cals_in", "avg7"),
    ("Avg protein g (7d)", "total_protein_g", "avg7"),
    ("Avg carbs g (7d)", "total_carbs_g", "avg7"),
    ("Avg fat g (7d)", "total_fat_g", "avg7"),
    ("Nutrition days in window", "", "days7"),
    ("Bowel movements (last 7 days)", "bowel_movement", "count7"),
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
                range=f"{tab}!A1:{col_letter(51)}",  # A:AZ
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
