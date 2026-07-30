"""A durable, local stand-in for the Cloud Tasks `meal-ingest` queue.

The queue is not a detail of this system — it *is* the insertion guarantee. A meal
photo is acknowledged in seconds and analysed later, so if the queue loses a task
the meal is gone with no row and no stub. Everything here exists to make that as
hard as it is on Cloud Tasks.

**The semantics are copied, not reinvented**, because `/process` is written against
them and is deliberately left unchanged (see `main._worker_kwargs`, which spends
the first 6 of 8 attempts on the best model alone):

| Cloud Tasks (`meal-ingest`) | here |
|---|---|
| `maxAttempts: 8` | `LOCAL_QUEUE_MAX_ATTEMPTS` |
| backoff 5 s → 120 s, doubling | `_backoff_s` |
| `maxRetryDuration: 900s` | counted from the **first dispatch**, not from enqueue |
| 1 concurrent dispatch | one worker process, `LIMIT 1` |
| `X-CloudTasks-TaskRetryCount`, 0-based | sent verbatim, same header name |
| `schedule_time` (coach debounce) | `not_before` |

Two deliberate choices worth knowing:

* **A separate process, not a thread inside Flask.** Cloud Tasks is an external
  dispatcher that calls the service over HTTP, and keeping that shape means
  `/process` needs no edit at all. It also survives the web service: restarting
  gunicorn (a redeploy, a crash, an edit) must not drop the tasks waiting on their
  backoff, and a task mid-analysis when the API restarts should be retried rather
  than lost. Both processes are guarded by `LIMIT 1` + a single worker, which is
  what "1 concurrent dispatch" means here.
* **Retry count is the number of dispatches already made** (so the first dispatch
  sends `0`). Off by one here and `_worker_kwargs` would either burn the patience
  budget early or never write the final stub — the two failure modes the retry
  window was designed around.

Only stdlib: sqlite3 + urllib. The queue must not be the thing that can't start
because a wheel failed to build.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("localqueue")

# Mirrors the `meal-ingest` queue config. TASKS_MAX_ATTEMPTS in main.py must stay
# <= this, for exactly the reason documented there: the worker is what ends the
# loop by writing a stub, so if the queue gives up first the meal vanishes.
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_MIN_BACKOFF_S = 5.0
DEFAULT_MAX_BACKOFF_S = 120.0
DEFAULT_MAX_RETRY_DURATION_S = 900.0
# Cloud Run's request timeout on health-tracker-ingest was 180s and the analysis
# budget (GEMINI_DEADLINE_S 105 + one 60s call + ~5s of writes) is sized to fit
# inside it. Keep them equal so a slow task fails the same way it used to.
DEFAULT_DISPATCH_TIMEOUT_S = 180.0
# How long to sleep when there is nothing due. Short enough that a meal queued
# now starts analysing promptly, long enough to be free at idle.
DEFAULT_POLL_INTERVAL_S = 1.0


def db_path() -> Path:
    """Where the queue lives. Under $XDG_STATE_HOME by default so it survives a
    restart — /tmp would make a reboot mid-analysis lose the meal."""
    raw = os.environ.get("LOCAL_QUEUE_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    state = os.environ.get("XDG_STATE_HOME", "").strip() or "~/.local/state"
    return Path(state).expanduser() / "health-tracker" / "queue.db"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the Flask process enqueue while the worker is mid-dispatch instead
    # of one blocking the other on a write lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")   # durability > speed; this is the guarantee
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    """Create the schema. Safe to call on every process start."""
    own = conn is None
    conn = conn or _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                url               TEXT    NOT NULL,
                headers           TEXT    NOT NULL,
                body              BLOB    NOT NULL,
                enqueued_at       REAL    NOT NULL,
                not_before        REAL    NOT NULL,
                first_dispatch_at REAL,
                attempts          INTEGER NOT NULL DEFAULT 0,
                state             TEXT    NOT NULL DEFAULT 'pending',
                last_error        TEXT,
                updated_at        REAL    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(state, not_before)")
    finally:
        if own:
            conn.close()


def enqueue(url: str, body: bytes, headers: Optional[Dict[str, str]] = None,
            *, delay_s: float = 0.0,
            conn: Optional[sqlite3.Connection] = None) -> int:
    """Persist one task and return its id.

    Raises on any storage failure rather than swallowing it: `/ingest` catches the
    exception and writes a failure stub, so a broken queue degrades to the old
    synchronous behaviour instead of silently dropping the meal.
    """
    own = conn is None
    conn = conn or _connect()
    try:
        init_db(conn)
        now = time.time()
        cur = conn.execute(
            "INSERT INTO tasks (url, headers, body, enqueued_at, not_before, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (url, json.dumps(headers or {}), body, now, now + max(0.0, delay_s), now))
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def max_attempts() -> int:
    return _int_env("LOCAL_QUEUE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def _backoff_s(attempts: int) -> float:
    """Cloud Tasks' schedule: min_backoff doubling per retry, capped at max_backoff.

    `attempts` is the number of dispatches already made, so the wait after the
    first failure (attempts=1) is min_backoff. No jitter: Cloud Tasks doesn't
    jitter either, and the in-attempt Gemini retries already do (see
    DEFAULT_BACKOFF_JITTER_S in main.py) — adding it here as well would push the
    measured ~6 calls/min rate around for no reason.
    """
    lo = _float_env("LOCAL_QUEUE_MIN_BACKOFF_S", DEFAULT_MIN_BACKOFF_S)
    hi = _float_env("LOCAL_QUEUE_MAX_BACKOFF_S", DEFAULT_MAX_BACKOFF_S)
    return min(hi, lo * (2 ** max(0, attempts - 1)))


def claim_due(conn: sqlite3.Connection, *, now: Optional[float] = None
              ) -> Optional[sqlite3.Row]:
    """Return the next task whose `not_before` has passed, oldest first.

    LIMIT 1 plus a single worker process is what implements "1 concurrent
    dispatch". Do not relax it: `upsert_daily` is read-modify-write against a grid
    snapshot, so two overlapping executions can append the same date twice.
    """
    now = time.time() if now is None else now
    cur = conn.execute(
        "SELECT * FROM tasks WHERE state = 'pending' AND not_before <= ? "
        "ORDER BY not_before, id LIMIT 1", (now,))
    return cur.fetchone()


def dispatch(task: sqlite3.Row, *, timeout_s: Optional[float] = None
             ) -> Tuple[int, str]:
    """POST the task to its url. Returns (status_code, detail).

    A transport failure is reported as status 0 so the caller treats it exactly
    like a 5xx — retryable. That matters on a laptop, where the service being
    briefly down (a restart, a redeploy) is a normal event rather than an outage.
    """
    timeout_s = timeout_s or _float_env("LOCAL_QUEUE_DISPATCH_TIMEOUT_S",
                                        DEFAULT_DISPATCH_TIMEOUT_S)
    headers = dict(json.loads(task["headers"] or "{}"))
    # 0-based, same as Cloud Tasks: `attempts` counts dispatches already made.
    headers["X-CloudTasks-TaskRetryCount"] = str(int(task["attempts"]))
    headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(task["url"], data=bytes(task["body"]),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), (resp.read(200) or b"").decode(
                "utf-8", "replace")
    except urllib.error.HTTPError as err:
        return int(err.code), (err.read(200) or b"").decode("utf-8", "replace")
    except Exception as err:                      # transport, DNS, timeout, reset
        return 0, f"{type(err).__name__}: {err}"


def _finish(conn: sqlite3.Connection, task_id: int, state: str,
            detail: str) -> None:
    conn.execute(
        "UPDATE tasks SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (state, detail[:500], time.time(), task_id))


def run_once(conn: sqlite3.Connection, *, now: Optional[float] = None,
             on_dead: Optional[Any] = None) -> bool:
    """Dispatch at most one due task. Returns True if one was attempted.

    The task is marked as dispatched (attempts += 1, first_dispatch_at pinned)
    BEFORE the HTTP call, so a worker killed mid-dispatch resumes with the attempt
    counted rather than replaying it forever. Cloud Tasks makes the same trade:
    at-least-once with a bounded count, never unbounded.
    """
    task = claim_due(conn, now=now)
    if task is None:
        return False

    started = time.time()
    first = task["first_dispatch_at"] or started
    conn.execute(
        "UPDATE tasks SET attempts = attempts + 1, first_dispatch_at = ?, "
        "updated_at = ? WHERE id = ?", (first, started, task["id"]))
    attempts_made = int(task["attempts"]) + 1

    status, detail = dispatch(task)
    if 200 <= status < 300:
        _finish(conn, task["id"], "done", f"{status}")
        log.info("task %s done (%s) after %d attempt(s)",
                 task["id"], status, attempts_made)
        return True

    # Give-up conditions, both of which Cloud Tasks enforces itself.
    cap = _float_env("LOCAL_QUEUE_MAX_RETRY_DURATION_S",
                     DEFAULT_MAX_RETRY_DURATION_S)
    exhausted = attempts_made >= max_attempts()
    expired = (time.time() - first) >= cap
    if exhausted or expired:
        why = "attempts exhausted" if exhausted else "retry window expired"
        _finish(conn, task["id"], "dead", f"{why}: {status} {detail}")
        log.error("task %s DEAD (%s) after %d attempt(s): %s %s",
                  task["id"], why, attempts_made, status, detail[:200])
        if on_dead:
            try:
                on_dead(task, why, status, detail)
            except Exception:
                log.exception("on_dead hook failed")
        return True

    wait = _backoff_s(attempts_made)
    conn.execute(
        "UPDATE tasks SET not_before = ?, last_error = ?, updated_at = ? "
        "WHERE id = ?",
        (time.time() + wait, f"{status} {detail}"[:500], time.time(), task["id"]))
    log.warning("task %s attempt %d got %s; retrying in %.0fs",
                task["id"], attempts_made, status, wait)
    return True


def purge_done(conn: sqlite3.Connection, *, older_than_s: float = 7 * 86400
               ) -> int:
    """Drop completed rows after a week. Dead ones are KEPT — a dead task is the
    record that a meal was lost, and that must outlive the retention window."""
    cutoff = time.time() - older_than_s
    cur = conn.execute(
        "DELETE FROM tasks WHERE state = 'done' AND updated_at < ?", (cutoff,))
    return cur.rowcount or 0


def stats(conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    """Counts per state — used by the health check and `agents-doctor`."""
    own = conn is None
    conn = conn or _connect()
    try:
        init_db(conn)
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
        out = {"pending": 0, "done": 0, "dead": 0}
        for row in rows:
            out[str(row["state"])] = int(row["n"])
        return out
    finally:
        if own:
            conn.close()
