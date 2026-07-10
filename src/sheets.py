"""Google Sheets "gold" writer: upsert one row per day, keyed on date.

Uses the Cloud Run runtime service account (Application Default Credentials)
scoped to spreadsheets. The target Sheet must be shared (Editor) with that
service account's email.
"""
from __future__ import annotations

from typing import Any, Dict, List

from googleapiclient.discovery import build

HEADERS = ["date", "weight_kg", "body_fat_pct", "source", "updated_at"]


class SheetWriter:
    def __init__(self, creds, spreadsheet_id: str, tab: str = "daily"):
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sid = spreadsheet_id
        self.tab = tab

    # -- schema helpers -------------------------------------------------
    def ensure_tab(self) -> None:
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        if self.tab not in titles:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"addSheet": {"properties": {"title": self.tab}}}]},
            ).execute()

    def ensure_header(self) -> None:
        rng = f"{self.tab}!A1:{chr(ord('A') + len(HEADERS) - 1)}1"
        resp = (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sid, range=rng)
            .execute()
        )
        if not resp.get("values"):
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid,
                range=f"{self.tab}!A1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()

    # -- upsert ---------------------------------------------------------
    def upsert_rows(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Insert new dates, update existing ones. Idempotent on `date`."""
        existing = (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sid, range=f"{self.tab}!A2:A")
            .execute()
            .get("values", [])
        )
        date_to_row = {r[0]: i + 2 for i, r in enumerate(existing) if r}
        last_col = chr(ord("A") + len(HEADERS) - 1)

        appends: List[List[Any]] = []
        updated = 0
        for row in rows:
            values = [row.get(h) for h in HEADERS]
            if row["date"] in date_to_row:
                n = date_to_row[row["date"]]
                self.svc.spreadsheets().values().update(
                    spreadsheetId=self.sid,
                    range=f"{self.tab}!A{n}:{last_col}{n}",
                    valueInputOption="RAW",
                    body={"values": [values]},
                ).execute()
                updated += 1
            else:
                appends.append(values)

        if appends:
            self.svc.spreadsheets().values().append(
                spreadsheetId=self.sid,
                range=f"{self.tab}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": appends},
            ).execute()

        return {"updated": updated, "appended": len(appends)}
