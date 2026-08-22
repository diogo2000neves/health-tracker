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

import logging
import random
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from schema.registry import daily_headers, names_in, ocr_ranges

log = logging.getLogger("sheets")

DAILY_TAB = "daily_summary"
MEALS_TAB = "meals"
SCHEMA_TAB = "schema"

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

# Micronutrients that get a daily total column (`total_<key>`). The full 32-nutrient
# set lives per-ingredient in the meals `items` JSON; this is the high-value subset
# that additionally rolls up.
TIER1_NUTRIENTS: List[str] = [
    n[len("total_"):] for n in names_in("nutrition")
    if n.startswith("total_") and n not in
    ("total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g")
]



# Merge-upsert keys this column; everything else in a row is left to its owner.
_DAILY_KEY = "date"

# Transient server-side answers. A 503 from Sheets is the one actually observed:
# it failed the whole daily sync on 2026-08-19 and again on 2026-08-20, on the
# very first call (`spreadsheets.get`, before a single row had been touched).
# Nothing here was retried, and unlike the ingest service — whose every sheet call
# sits inside a task the queue re-runs 8 times — the daily job has no outer net at
# all, so one blip from Google was a lost run. The trailing HEALTH_RECONCILE_DAYS
# window meant the data healed the next morning, which is exactly why this went
# unnoticed for two days while the alert fired both times.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# A dead keep-alive socket surfaces as one of these, and the daily job is the most
# exposed thing there is to it: it reads the sheet, then spends minutes fetching
# Google Health and running the model, then writes — by which point the connection
# it opened at the start is long gone. See `ingest/main.py:_per_thread` for the
# measurements (idle 60 s -> the read blocks for the FULL socket timeout).
_CONN_ERRORS = (ConnectionError, socket.timeout, ssl.SSLError)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5
_BACKOFF_CAP_S = 20.0


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

    # Class-level default so the tests' `SheetClient.__new__(SheetClient)` (which
    # skips __init__ deliberately, to stub `svc`) still has the attribute.
    _creds = None

    def __init__(self, creds, spreadsheet_id: str):
        self._creds = creds
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sid = spreadsheet_id
        self._titles: Optional[set] = None

    def _rebuild(self) -> None:
        """Drop the current client so the next attempt gets a live socket.

        Only possible when we still hold the credentials; a test-stubbed client
        keeps whatever it was given.
        """
        if self._creds is not None:
            self.svc = build("sheets", "v4", credentials=self._creds,
                             cache_discovery=False)

    def _execute(self, make_request: Callable[[], Any], *,
                 idempotent: bool = True) -> Any:
        """Run `make_request().execute()`, riding through a transient failure.

        `make_request` must build a *fresh* request each call, so a retry picks up
        the rebuilt client rather than the one holding the dead socket.

        ⚠️ `idempotent=False` for anything that APPENDS or DELETES — the same rule
        `ingest/main.py:_execute` spells out, and for the same reason: a connection
        error means the response could not be READ, never that the request was not
        performed. Google may well have appended the row before the socket broke,
        so a retry writes it twice (that is how one lunch became two rows on
        2026-08-21), and re-running a `deleteDimension` deletes whatever has since
        shifted into those indices. Those calls fail fast and let the caller — or
        the next run's reconcile window — sort it out.
        """
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                return make_request().execute()
            except HttpError as err:
                status = getattr(getattr(err, "resp", None), "status", None)
                if status not in _RETRYABLE_STATUSES or last or not idempotent:
                    raise
                log.warning("sheets %s on attempt %d/%d; retrying",
                            status, attempt + 1, _MAX_ATTEMPTS)
            except _CONN_ERRORS as err:
                if not idempotent:
                    log.warning("connection lost on a non-idempotent write; NOT "
                                "retrying here (it may already have landed): %s", err)
                    raise
                if last:
                    raise
                log.warning("stale sheets connection, reconnecting (%d/%d): %s",
                            attempt + 1, _MAX_ATTEMPTS, err)
                self._rebuild()
            # Jitter keeps a retry storm from lining up with whatever else on this
            # laptop is talking to the same quota.
            time.sleep(min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** attempt))
                       + random.uniform(0, 0.5))

    # -- structure ------------------------------------------------------
    def tab_titles(self, refresh: bool = False) -> set:
        if self._titles is None or refresh:
            meta = self._execute(
                lambda: self.svc.spreadsheets().get(spreadsheetId=self.sid))
            self._titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        return self._titles

    def ensure_tab(self, title: str, headers: Sequence[str]) -> None:
        """Create the tab if missing and (re)write its header row if it drifts."""
        if title not in self.tab_titles():
            # Not retried: addSheet on a title that already exists is a 400, so a
            # retry after a success we failed to read turns a blip into a hard error.
            self._execute(lambda: self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
            ), idempotent=False)
            self._titles = None
        rng = f"{title}!A1:{col_letter(len(headers) - 1)}1"
        current = self._execute(
            lambda: self.svc.spreadsheets().values()
            .get(spreadsheetId=self.sid, range=rng)
        ).get("values", [[]])
        if not current or current[0] != list(headers):
            self._execute(lambda: self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{title}!A1",
                valueInputOption="RAW", body={"values": [list(headers)]},
            ))

    def sheet_id(self, tab: str) -> Optional[int]:
        """The tab's numeric sheetId (needed to sort it), or None if absent."""
        meta = self._execute(
            lambda: self.svc.spreadsheets().get(spreadsheetId=self.sid))
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
        # Sorting twice lands on the same order, so this is safe to repeat.
        self._execute(lambda: self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,
            body={"requests": [{"sortRange": {
                "range": {"sheetId": tab_id, "startRowIndex": 1,
                          "startColumnIndex": 0},
                "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
            }}]},
        ))

    def header(self, tab: str) -> List[str]:
        """The tab's header row as written in the sheet ([] if the tab is absent)."""
        if tab not in self.tab_titles():
            return []
        values = self._execute(
            lambda: self.svc.spreadsheets().values()
            .get(spreadsheetId=self.sid, range=f"{tab}!1:1")
        ).get("values", [[]])
        return values[0] if values else []

    # -- reads ----------------------------------------------------------
    def read_rows(self, tab: str) -> List[Dict[str, Any]]:
        """Return data rows as header-keyed dicts ([] if the tab is absent)."""
        if tab not in self.tab_titles():
            return []
        values = self._execute(
            lambda: self.svc.spreadsheets().values()
            .get(
                spreadsheetId=self.sid,
                range=f"{tab}!A1:{READ_LAST_COL}",
                valueRenderOption="UNFORMATTED_VALUE",
            )
        ).get("values", [])
        if len(values) < 2:
            return []
        headers = values[0]
        return [dict(zip(headers, row)) for row in values[1:]]

    # -- writes ----------------------------------------------------------
    def write_values(self, tab: str, a1: str, values: List[List[Any]],
                     user_entered: bool = False) -> None:
        """Write a block of values anchored at `a1` (e.g. stat cells, formulas)."""
        self._execute(lambda: self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid, range=f"{tab}!{a1}",
            valueInputOption="USER_ENTERED" if user_entered else "RAW",
            body={"values": values},
        ))

    def replace_rows(self, tab: str, headers: Sequence[str],
                     rows: List[List[Any]]) -> int:
        """Rebuild a *derived* tab from scratch: ensure it exists, clear it, write
        the header plus `rows`.

        For tabs that are a pure function of `daily_summary` (`calibration`, and
        `baselines` when it lands). Rebuilding beats incremental updates for these:
        a derived tab that is patched row-by-row can drift away from its source
        after a backfill or a corrected old meal, and the drift is invisible. The
        clear is what makes a shrunken result actually shrink — writing 20 rows
        over an old 40-row tab would otherwise leave 20 rows of stale garbage
        below, which reads as real data."""
        self.ensure_tab(tab, headers)
        # Both halves are absolute: clearing an already-clear range and rewriting
        # the same block land where they landed the first time.
        self._execute(lambda: self.svc.spreadsheets().values().clear(
            spreadsheetId=self.sid, range=f"{tab}!A2:ZZ", body={}))
        if rows:
            self._execute(lambda: self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{tab}!A2",
                valueInputOption="RAW", body={"values": rows},
            ))
        return len(rows)

    def append_row(self, tab: str, row: List[Any]) -> None:
        # The archetypal non-idempotent call: a retry here is a duplicate row.
        self._execute(lambda: self.svc.spreadsheets().values().append(
            spreadsheetId=self.sid, range=f"{tab}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ), idempotent=False)

    def _heal_daily_duplicates(self, grid: List[List[Any]]) -> List[List[Any]]:
        """Collapse rows that share a date, folding the extras' non-blank cells
        into the first occurrence and deleting them from the sheet.

        This job and the ingest service's `write_daily` are two independent
        read-modify-write writers against the same tab with no lock between them:
        a weigh-in writes the body columns for TODAY the instant it lands, but if
        this job's grid snapshot (taken here, before the merge below) was read a
        moment earlier — this run was already mid-flight (the 11:00 backstop, or a
        previous weigh-in's triggered run) when the weigh-in landed — it won't
        find that date yet and appends a second, half-empty row instead of merging
        into it. Healing on every run means the duplicate never survives past the
        next call here, rather than accumulating."""
        width = len(DAILY_HEADERS)
        survivor_idx: Dict[str, int] = {}     # date -> index into `grid`
        survivors: Dict[int, List[Any]] = {}  # grid index -> merged, padded row
        doomed_rownums: List[int] = []        # 1-based sheet rows to delete

        for i, row in enumerate(grid):
            if not row:
                continue
            day = str(row[0])
            padded = list(row) + [None] * (width - len(row))
            if day not in survivor_idx:
                survivor_idx[day] = i
                survivors[i] = padded
            else:
                target = survivors[survivor_idx[day]]
                for col in range(1, width):
                    if target[col] in (None, "") and padded[col] not in (None, ""):
                        target[col] = padded[col]
                doomed_rownums.append(i + 2)  # grid[0] is sheet row 2

        if not doomed_rownums:
            return grid

        data = [
            {"range": f"{DAILY_TAB}!A{i + 2}:{col_letter(width - 1)}{i + 2}",
             "values": [survivors[i]]}
            for i in survivor_idx.values()
        ]
        self._execute(lambda: self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption": "RAW", "data": data}))

        tab_id = self.sheet_id(DAILY_TAB)
        if tab_id is not None:
            # One batch, indices descending, so deleting a lower row never shifts
            # the sheet row number a later request in the same batch still refers to.
            delete_requests = [
                {"deleteDimension": {"range": {
                    "sheetId": tab_id, "dimension": "ROWS",
                    "startIndex": rownum - 1, "endIndex": rownum,
                }}}
                for rownum in sorted(doomed_rownums, reverse=True)
            ]
            # Not retried, and this is the sharpest case of the rule: the indices
            # are positional, so re-running a delete we could not confirm removes
            # whatever has since shifted up into those rows — real data, silently.
            self._execute(lambda: self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid, body={"requests": delete_requests}),
                idempotent=False)

        return [survivors[i] for i in sorted(survivor_idx.values())]

    # -- daily_summary: merge-upsert on date ----------------------------
    def upsert_daily(self, rows: List[Dict[str, Any]]) -> Dict[str, int]:
        """Merge rows into daily_summary keyed on `date`.

        Only fields present (and non-None) in a row overwrite existing cells, so
        a nutrition-only or body-only update never blanks columns owned by
        another source. New dates are appended.
        """
        width = len(DAILY_HEADERS)
        grid = self._execute(
            lambda: self.svc.spreadsheets().values()
            .get(
                spreadsheetId=self.sid,
                range=f"{DAILY_TAB}!A2:{col_letter(width - 1)}",
                # UNFORMATTED so existing numbers are read (and rewritten) as
                # numbers rather than locale strings that RAW would store as text.
                valueRenderOption="UNFORMATTED_VALUE",
            )
        ).get("values", [])
        healed = len(grid)
        grid = self._heal_daily_duplicates(grid)
        healed -= len(grid)
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
            # Every range is absolute (`A<rownum>:<last><rownum>`), so replaying
            # this writes the same cells the same values.
            self._execute(lambda: self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption": "RAW", "data": updates},
            ))
        if appends:
            # A repeat here would append the day a second time — the very thing
            # `_heal_daily_duplicates` exists to clean up after.
            self._execute(lambda: self.svc.spreadsheets().values().append(
                spreadsheetId=self.sid, range=f"{DAILY_TAB}!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": appends},
            ), idempotent=False)
        return {"updated": len(updates), "appended": len(appends), "healed": healed}
