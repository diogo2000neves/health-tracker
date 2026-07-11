"""Google Sheets access for the per-day health model.

* ``daily_summary`` — one row per calendar day holding the 24h "readiness ->
  output" vector (sleep/recovery stamped on the wake day) plus the day's nutrition
  roll-up and representative body composition. Rows are *merge-upserted* on
  ``date`` so independent sources (scale, meals and — later — Fitbit biometrics)
  each fill their own columns without clobbering the rest.

* ``meals`` — written by the ingest service; read here only as the granular
  nutrition source for the daily roll-up, and otherwise left untouched.

All access uses the Cloud Run runtime service account (ADC) scoped to
spreadsheets; the target Sheet must be shared (Editor) with that account.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from googleapiclient.discovery import build

DAILY_TAB = "daily_summary"
MEALS_TAB = "meals"

# Parent schema. The first block mirrors the blueprint's readiness vector; the
# body-composition block extends it so per-day physique is queryable alongside
# nutrition (the tracker's whole point), and `updated_at` is bookkeeping. Only
# ever *append* new columns here so historical rows stay aligned.
DAILY_HEADERS: List[str] = [
    "date",
    "sleep_score", "hrv_ms", "spo2_pct", "skin_temp_dev", "subjective_feel",
    "total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g",
    "total_active_mins", "steps",
    "weight_kg", "body_fat_pct",
    "updated_at",
]

# Merge-upsert keys this column; everything else in a row is left to its owner.
_DAILY_KEY = "date"


def _col(index: int) -> str:
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

    # -- structure ------------------------------------------------------
    def tab_titles(self) -> set:
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        return {s["properties"]["title"] for s in meta.get("sheets", [])}

    def ensure_tab(self, title: str, headers: Sequence[str]) -> None:
        """Create the tab if missing and (re)write its header row if it drifts."""
        if title not in self.tab_titles():
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ).execute()
        rng = f"{title}!A1:{_col(len(headers) - 1)}1"
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

    # -- reads ----------------------------------------------------------
    def read_rows(self, tab: str) -> List[Dict[str, str]]:
        """Return data rows as header-keyed dicts ([] if the tab is absent)."""
        if tab not in self.tab_titles():
            return []
        values = (
            self.svc.spreadsheets().values()
            .get(
                spreadsheetId=self.sid,
                range=f"{tab}!A1:{_col(51)}",  # A:AZ
                # UNFORMATTED so numbers come back as numbers, not locale-formatted
                # strings ("7.8" not "7,8") that would fail to parse / re-key.
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute().get("values", [])
        )
        if len(values) < 2:
            return []
        headers = values[0]
        return [dict(zip(headers, row)) for row in values[1:]]

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
                range=f"{DAILY_TAB}!A2:{_col(width - 1)}",
                # UNFORMATTED so existing numbers are read (and rewritten) as
                # numbers rather than locale strings that RAW would store as text.
                valueRenderOption="UNFORMATTED_VALUE",
            )
            .execute().get("values", [])
        )
        date_to_index = {r[0]: i for i, r in enumerate(grid) if r}
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
                    "range": f"{DAILY_TAB}!A{rownum}:{_col(width - 1)}{rownum}",
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
