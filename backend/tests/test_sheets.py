"""Unit tests for sheet schema constants and helpers."""
from src.biometrics import BIOMETRIC_COLUMNS
from src.sheets import (
    BODY_METRICS, DAILY_HEADERS, DAILY_TAB, READ_LAST_COL, SheetClient,
    TIER1_NUTRIENTS, col_letter,
)


def test_col_letter():
    assert col_letter(0) == "A"
    assert col_letter(25) == "Z"
    assert col_letter(26) == "AA"
    assert col_letter(51) == "AZ"


def test_daily_schema_shape():
    # `date` keys the merge-upsert; bookkeeping stays last; lean mass sits with
    # the physique block it derives from.
    assert DAILY_HEADERS[0] == "date"
    assert DAILY_HEADERS[-1] == "updated_at"
    i = DAILY_HEADERS.index
    assert i("weight_kg") < i("body_fat_pct") < i("lean_mass_kg") < i("updated_at")
    assert len(DAILY_HEADERS) == len(set(DAILY_HEADERS))  # no duplicates


def test_bowel_movement_is_a_self_reported_daily_flag():
    # a TRUE/blank marker that is self-reported about a day, not sensor data.
    assert "bowel_movement" in DAILY_HEADERS
    i = DAILY_HEADERS.index
    assert i("bowel_movement") == i("date") + 1
    assert DAILY_HEADERS[-1] == "updated_at"  # still last


def test_every_biometric_column_is_in_the_schema():
    for col in BIOMETRIC_COLUMNS:
        assert col in DAILY_HEADERS, col
    # one contiguous block, so maintenance can insert into it cleanly
    positions = [DAILY_HEADERS.index(c) for c in BIOMETRIC_COLUMNS]
    assert positions == list(range(positions[0], positions[0] + len(BIOMETRIC_COLUMNS)))


def test_there_is_no_sleep_score_column():
    # Fitbit's 0-100 score is proprietary and appears nowhere in the Google Health
    # API (verified field-by-field). A column for it could only ever stay blank —
    # sleep_efficiency_pct is the honest, derivable stand-in.
    assert "sleep_score" not in DAILY_HEADERS
    assert "sleep_efficiency_pct" in DAILY_HEADERS


def test_read_range_covers_the_whole_schema_with_headroom():
    # daily_summary silently outgrew A:Z once; a short read truncates the header so
    # columns past the cut look "missing" and their writes land nowhere.
    def index(letters):
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return n - 1
    assert index(READ_LAST_COL) >= len(DAILY_HEADERS) - 1 + 20


def test_tier1_nutrients_have_daily_columns():
    assert len(TIER1_NUTRIENTS) == 14
    for n in TIER1_NUTRIENTS:
        assert f"total_{n}" in DAILY_HEADERS
    # nutrient totals sit inside the nutrition block: after the macros they extend,
    # and before the body-composition block
    i = DAILY_HEADERS.index
    assert i("total_fat_g") < i("total_fiber_g") < i("weight_kg")


def test_untracked_nutrients_have_no_column_at_all():
    """Nutrients food isn't the main source of are gone from the schema entirely.

    No `total_*` column, no roll-up, no empty column left behind: the table holds
    only what we actually measure. See CONTEXT.md §2d for why these four went.
    """
    for n in ("vitamin_d_ug", "vitamin_k_ug", "biotin_ug", "chloride_mg"):
        assert f"total_{n}" not in DAILY_HEADERS
        assert n not in TIER1_NUTRIENTS


def test_every_scale_metric_has_a_column():
    # The ten metrics the scale computes from bioimpedance. The Google Health API
    # only ever exposed the first three — Fitbit strips the rest on the way through
    # — which is the whole reason the screenshot replaced it.
    assert len(BODY_METRICS) == 10
    for metric in BODY_METRICS:
        assert metric in DAILY_HEADERS
    # one contiguous block, so maintenance.py inserts new columns into it cleanly
    positions = [DAILY_HEADERS.index(m) for m in BODY_METRICS]
    assert positions == list(range(positions[0], positions[0] + len(BODY_METRICS)))
    # the derived and stamped columns close the block
    i = DAILY_HEADERS.index
    assert i("metabolic_age") < i("lean_mass_kg") < i("body_measured_at")


class _FakeSheetsSvc:
    """Records the two request shapes `_heal_daily_duplicates` issues, keyed by
    whether `.values()` was chained before `.batchUpdate()`/`.get()` (a values
    write) or not (a structural request, or the metadata read for `sheet_id`)."""

    def __init__(self):
        self.value_batch_bodies = []
        self.sheet_batch_bodies = []
        self._in_values = False

    def spreadsheets(self):
        self._in_values = False
        return self

    def values(self):
        self._in_values = True
        return self

    def get(self, spreadsheetId):
        return self

    def batchUpdate(self, spreadsheetId, body):
        (self.value_batch_bodies if self._in_values else self.sheet_batch_bodies).append(body)
        return self

    def execute(self):
        return {"sheets": [{"properties": {"title": DAILY_TAB, "sheetId": 7}}]}


def _client(svc):
    client = SheetClient.__new__(SheetClient)
    client.svc = svc
    client.sid = "sid"
    client._titles = None
    return client


def test_heal_daily_duplicates_merges_and_deletes_the_extra_row():
    # Reproduces the reported bug: a weigh-in's own row (weight_kg only) and the
    # daily job's row (sleep_mins only) both landed under the same date because
    # the job's grid snapshot predated the weigh-in's write.
    width = len(DAILY_HEADERS)
    weight_i, sleep_i = DAILY_HEADERS.index("weight_kg"), DAILY_HEADERS.index("sleep_mins")
    row_a, row_b = [None] * width, [None] * width
    row_a[0], row_a[weight_i] = "2026-07-19", 70.5
    row_b[0], row_b[sleep_i] = "2026-07-19", 420

    svc = _FakeSheetsSvc()
    healed = _client(svc)._heal_daily_duplicates([row_a, row_b])

    assert len(healed) == 1
    assert healed[0][0] == "2026-07-19"
    assert healed[0][weight_i] == 70.5
    assert healed[0][sleep_i] == 420
    # the merge was written back to the surviving row (sheet row 2, since a
    # bare grid — no header — starts data at row 2)...
    assert len(svc.value_batch_bodies) == 1
    assert svc.value_batch_bodies[0]["data"][0]["range"] == \
        f"{DAILY_TAB}!A2:{col_letter(width - 1)}2"
    # ...and the duplicate (sheet row 3) was deleted
    assert svc.sheet_batch_bodies == [{"requests": [{"deleteDimension": {"range": {
        "sheetId": 7, "dimension": "ROWS", "startIndex": 2, "endIndex": 3,
    }}}]}]


def test_heal_daily_duplicates_is_a_noop_without_duplicates():
    width = len(DAILY_HEADERS)
    row = [None] * width
    row[0] = "2026-07-19"

    svc = _FakeSheetsSvc()
    healed = _client(svc)._heal_daily_duplicates([row])

    assert healed == [row]
    assert svc.value_batch_bodies == []
    assert svc.sheet_batch_bodies == []



# -- transient-failure resilience: the 2026-08-19/20 lost daily syncs ----------
#
# Both runs died on the FIRST call (`spreadsheets.get`) with a 503 from Google,
# before a single row had been touched. Nothing retried it. The ingest service
# survives the same blip only because every sheet call there sits inside a task
# the queue re-runs 8 times; the daily job has no outer net at all, so one blip
# was a lost run — invisible, because the trailing reconcile window healed the
# data the next morning while the alert fired both times.

class _FlakySvc:
    """Fails the first `fails` attempts with `err`, then succeeds."""

    def __init__(self, err, fails=1, reply=None):
        self._err, self._fails, self._reply = err, fails, reply or {}
        self.attempts = 0

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        return self

    def append(self, **kwargs):
        return self

    def batchUpdate(self, **kwargs):
        return self

    def execute(self):
        self.attempts += 1
        if self.attempts <= self._fails:
            raise self._err
        return self._reply


def _http_error(status):
    from googleapiclient.errors import HttpError
    return HttpError(type("R", (), {"status": status, "reason": "x"})(), b"{}")


def _sheet_over(svc):
    client = SheetClient.__new__(SheetClient)
    client.svc = svc
    client.sid = "sid"
    client._titles = None
    return client


def test_a_503_no_longer_loses_the_whole_daily_sync(monkeypatch):
    monkeypatch.setattr("src.sheets.time.sleep", lambda _s: None)
    svc = _FlakySvc(_http_error(503), fails=2,
                    reply={"sheets": [{"properties": {"title": DAILY_TAB}}]})
    assert _sheet_over(svc).tab_titles() == {DAILY_TAB}
    assert svc.attempts == 3


def test_a_dead_socket_is_retried_on_a_read(monkeypatch):
    """The daily job reads, then spends minutes on Google Health and the model,
    then writes — by which point its connection is long gone. See
    `ingest/main.py:_per_thread` for the measurements."""
    monkeypatch.setattr("src.sheets.time.sleep", lambda _s: None)
    svc = _FlakySvc(ConnectionResetError("socket died"), fails=1,
                    reply={"values": [["date"], ["2026-08-22"]]})
    client = _sheet_over(svc)
    client._titles = {DAILY_TAB}          # so the read is the only call measured
    assert client.read_rows(DAILY_TAB) == [{"date": "2026-08-22"}]
    assert svc.attempts == 2


def test_a_permanent_error_is_not_retried(monkeypatch):
    """A 403 is a misconfiguration, not a blip — retrying it just delays the alert."""
    import pytest
    monkeypatch.setattr("src.sheets.time.sleep", lambda _s: None)
    svc = _FlakySvc(_http_error(403), fails=99)
    with pytest.raises(Exception):
        _sheet_over(svc).tab_titles()
    assert svc.attempts == 1


def test_an_append_is_never_retried_however_transient_the_failure(monkeypatch):
    """THE rule. A connection error means the response could not be READ, never
    that the row was not written — so a retry is how one lunch becomes two rows
    (2026-08-21). Appends fail fast and let the caller decide."""
    import pytest
    monkeypatch.setattr("src.sheets.time.sleep", lambda _s: None)
    for err in (ConnectionResetError("died"), _http_error(503)):
        svc = _FlakySvc(err, fails=1, reply={})
        with pytest.raises(Exception):
            _sheet_over(svc).append_row(DAILY_TAB, ["2026-08-22"])
        assert svc.attempts == 1, f"{err!r} must not be retried on an append"
