"""Unit tests for the local stand-in for the Cloud Tasks `meal-ingest` queue.

What is pinned here is the *contract `/process` is written against*, not the
implementation: the 0-based retry count, the 5→120 s doubling backoff, the 8-attempt
ceiling and the 900 s window. `main._worker_kwargs` reads the retry count to decide
which models it may call, so an off-by-one here would either burn the patience
budget on the first attempt or never write the final "analysis failed" stub — the
two ways a meal goes missing.
"""
import pathlib
import sqlite3
import sys

import pytest

# The ingest modules import each other by flat name (the image flattens them into
# /app), so the ingest directory goes on the path.
_INGEST = pathlib.Path(__file__).resolve().parent.parent / "ingest"
if str(_INGEST) not in sys.path:
    sys.path.insert(0, str(_INGEST))

import localqueue  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A queue on a scratch DB. Pointing LOCAL_QUEUE_DB at tmp_path also proves
    the env override works — the real path is under XDG_STATE_HOME."""
    monkeypatch.setenv("LOCAL_QUEUE_DB", str(tmp_path / "queue.db"))
    c = localqueue._connect()
    localqueue.init_db(c)
    yield c
    c.close()


def _enqueue(conn, url="http://127.0.0.1:8080/process", body=b'{"a":1}', **kw):
    return localqueue.enqueue(url, body, {"X-Auth-Token": "t"}, conn=conn, **kw)


# -- backoff -------------------------------------------------------------------
def test_backoff_matches_cloud_tasks_schedule(conn):
    """5, 10, 20, 40, 80, then capped at 120 — the `meal-ingest` config."""
    got = [localqueue._backoff_s(n) for n in range(1, 9)]
    assert got == [5, 10, 20, 40, 80, 120, 120, 120]


def test_backoff_respects_env_overrides(conn, monkeypatch):
    monkeypatch.setenv("LOCAL_QUEUE_MIN_BACKOFF_S", "1")
    monkeypatch.setenv("LOCAL_QUEUE_MAX_BACKOFF_S", "4")
    assert [localqueue._backoff_s(n) for n in range(1, 5)] == [1, 2, 4, 4]


# -- the retry-count contract --------------------------------------------------
def test_success_marks_done_after_one_dispatch(conn, monkeypatch):
    monkeypatch.setattr(localqueue, "dispatch", lambda t, **k: (200, "ok"))
    _enqueue(conn)
    localqueue.run_once(conn)
    row = conn.execute("SELECT attempts, state FROM tasks").fetchone()
    assert row["state"] == "done"
    assert row["attempts"] == 1          # one dispatch made


def test_dispatch_sets_zero_based_retry_header(conn, monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self, _n=None):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(localqueue.urllib.request, "urlopen", fake_urlopen)
    _enqueue(conn)
    task = localqueue.claim_due(conn)
    localqueue.dispatch(task)
    # urllib title-cases header names.
    assert captured["headers"]["X-cloudtasks-taskretrycount"] == "0"

    conn.execute("UPDATE tasks SET attempts = 3")
    task = localqueue.claim_due(conn)
    localqueue.dispatch(task)
    assert captured["headers"]["X-cloudtasks-taskretrycount"] == "3"


# -- retry / give-up behaviour -------------------------------------------------
def test_failure_reschedules_with_backoff_not_death(conn, monkeypatch):
    monkeypatch.setattr(localqueue, "dispatch", lambda t, **k: (503, "overloaded"))
    _enqueue(conn)
    before = __import__("time").time()
    localqueue.run_once(conn)
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    # First failure waits min_backoff (5 s), so it is not immediately due again.
    assert row["not_before"] >= before + 4
    assert localqueue.claim_due(conn) is None


def test_dies_after_max_attempts_and_alerts(conn, monkeypatch):
    """The queue must stop at 8 so the worker's own stub-writing attempt is the
    last word. If the queue outlasted the worker the meal would be dropped with no
    stub and no row — the failure `TASKS_MAX_ATTEMPTS` exists to prevent."""
    monkeypatch.setattr(localqueue, "dispatch", lambda t, **k: (500, "boom"))
    monkeypatch.setenv("LOCAL_QUEUE_MIN_BACKOFF_S", "0")
    monkeypatch.setenv("LOCAL_QUEUE_MAX_BACKOFF_S", "0")
    dead = []
    _enqueue(conn)
    for _ in range(localqueue.DEFAULT_MAX_ATTEMPTS):
        localqueue.run_once(conn, on_dead=lambda *a: dead.append(a))

    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "dead"
    assert row["attempts"] == localqueue.DEFAULT_MAX_ATTEMPTS
    assert len(dead) == 1
    assert "attempts exhausted" in dead[0][1]


def test_retry_window_expiry_kills_before_attempts_run_out(conn, monkeypatch):
    """maxRetryDuration is the other give-up condition, counted from the FIRST
    dispatch rather than from enqueue — a task delayed by the coach debounce must
    not have its retry window consumed while it sits waiting."""
    monkeypatch.setattr(localqueue, "dispatch", lambda t, **k: (500, "boom"))
    monkeypatch.setenv("LOCAL_QUEUE_MIN_BACKOFF_S", "0")
    monkeypatch.setenv("LOCAL_QUEUE_MAX_RETRY_DURATION_S", "0")
    _enqueue(conn)
    localqueue.run_once(conn)
    row = conn.execute("SELECT * FROM tasks").fetchone()
    assert row["state"] == "dead"
    assert row["attempts"] == 1               # died on the window, not the count
    assert "retry window expired" in row["last_error"]


def test_transport_error_is_retryable_like_a_5xx(conn, monkeypatch):
    """The service being briefly down is normal on a laptop (a restart, an edit).
    It must be a retry, never a death."""
    def blow_up(req, timeout=None):
        raise ConnectionRefusedError("nothing listening")

    monkeypatch.setattr(localqueue.urllib.request, "urlopen", blow_up)
    _enqueue(conn)
    task = localqueue.claim_due(conn)
    status, detail = localqueue.dispatch(task)
    assert status == 0
    assert "ConnectionRefusedError" in detail

    localqueue.run_once(conn)
    assert conn.execute("SELECT state FROM tasks").fetchone()["state"] == "pending"


def test_4xx_is_not_retried_forever_but_still_counts(conn, monkeypatch):
    """A 401 is retried like anything else — the token may have been rotated
    mid-flight — but it still runs out of attempts rather than looping."""
    monkeypatch.setattr(localqueue, "dispatch", lambda t, **k: (401, "unauthorized"))
    monkeypatch.setenv("LOCAL_QUEUE_MIN_BACKOFF_S", "0")
    monkeypatch.setenv("LOCAL_QUEUE_MAX_BACKOFF_S", "0")
    _enqueue(conn)
    for _ in range(localqueue.DEFAULT_MAX_ATTEMPTS):
        localqueue.run_once(conn)
    assert conn.execute("SELECT state FROM tasks").fetchone()["state"] == "dead"


# -- scheduling / ordering -----------------------------------------------------
def test_delay_defers_the_task(conn):
    """The coach debounce schedules a generation an hour out; `not_before` is how
    that survives without Cloud Tasks' schedule_time."""
    _enqueue(conn, delay_s=3600)
    assert localqueue.claim_due(conn) is None
    assert localqueue.claim_due(conn, now=__import__("time").time() + 3601) is not None


def test_due_tasks_come_out_oldest_first(conn):
    first = _enqueue(conn, body=b'{"n":1}')
    second = _enqueue(conn, body=b'{"n":2}')
    assert localqueue.claim_due(conn)["id"] == first
    conn.execute("UPDATE tasks SET state='done' WHERE id=?", (first,))
    assert localqueue.claim_due(conn)["id"] == second


def test_run_once_reports_idle_when_nothing_due(conn):
    assert localqueue.run_once(conn) is False


# -- retention -----------------------------------------------------------------
def test_purge_drops_done_but_keeps_dead(conn):
    """A dead row is the record that a meal was lost; it must outlive retention."""
    done = _enqueue(conn)
    dead = _enqueue(conn)
    old = __import__("time").time() - 30 * 86400
    conn.execute("UPDATE tasks SET state='done', updated_at=? WHERE id=?", (old, done))
    conn.execute("UPDATE tasks SET state='dead', updated_at=? WHERE id=?", (old, dead))

    assert localqueue.purge_done(conn) == 1
    states = [r["state"] for r in conn.execute("SELECT state FROM tasks")]
    assert states == ["dead"]


def test_stats_counts_each_state(conn):
    a, b = _enqueue(conn), _enqueue(conn)
    conn.execute("UPDATE tasks SET state='dead' WHERE id=?", (b,))
    assert localqueue.stats(conn) == {"pending": 1, "done": 0, "dead": 1}


# -- durability ----------------------------------------------------------------
def test_enqueue_survives_a_reconnect(conn, tmp_path, monkeypatch):
    """The queue is the insertion guarantee, so it has to be on disk — an
    in-memory queue would lose every pending meal on a reboot."""
    _enqueue(conn, body=b'{"meal":"yes"}')
    conn.close()
    fresh = localqueue._connect()
    try:
        row = localqueue.claim_due(fresh)
        assert row is not None
        assert bytes(row["body"]) == b'{"meal":"yes"}'
    finally:
        fresh.close()


def test_attempt_is_counted_before_dispatch(conn, monkeypatch):
    """A worker killed mid-POST must resume with the attempt spent, not replay it
    forever. Asserted by observing the row from inside the dispatch call."""
    observed = {}

    def peek(task, **kw):
        observed["attempts"] = conn.execute(
            "SELECT attempts FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
        raise KeyboardInterrupt("killed mid-dispatch")

    monkeypatch.setattr(localqueue, "dispatch", peek)
    _enqueue(conn)
    with pytest.raises(KeyboardInterrupt):
        localqueue.run_once(conn)
    assert observed["attempts"] == 1
