"""The local queue's dispatcher — the process that replaces Cloud Tasks.

Runs as its own systemd user unit (`health-tracker-queue.service`) rather than a
thread inside Flask, for the reasons in `localqueue`'s docstring: it keeps the
external-dispatcher shape that lets `/process` stay untouched, and it outlives a
gunicorn restart so a backing-off task is never dropped by a redeploy.

    python queue_worker.py            # the service
    python queue_worker.py --once     # dispatch one due task and exit
    python queue_worker.py --stats    # counts per state

Run from `backend/ingest` (or with it on PYTHONPATH): the ingest modules import
each other by flat name because the container image flattens them into /app.

A dead task means a meal was lost after the full retry window. That is the one
event here worth waking someone for, so it goes to Telegram through the
`agents-notify` the rest of this machine already uses; if that fails, the error is
still in the journal and the row stays in the DB marked `dead` forever.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any

import localqueue

log = logging.getLogger("queue-worker")

# Purge completed rows once an hour rather than on every tick — it is a DELETE
# over an indexed range, cheap, but pointless at 1 Hz.
PURGE_INTERVAL_S = 3600.0

_stop = False


def _handle_signal(signum: int, _frame: Any) -> None:
    """Finish the in-flight dispatch, then exit. systemd sends SIGTERM on restart,
    and tearing down mid-POST would leave the attempt counted but unanswered."""
    global _stop
    _stop = True
    log.info("signal %s received; stopping after the current task", signum)


def notify_dead(task: Any, why: str, status: int, detail: str) -> None:
    """Tell the operator a meal was lost. Best-effort by design — an alert that
    can't be delivered must never take the worker down with it."""
    when = time.strftime("%H:%M", time.localtime(task["enqueued_at"]))
    msg = (f"health-tracker: task {task['id']} DEAD ({why}) — queued {when}, "
           f"last response {status} {detail[:120]}")
    cmd = os.environ.get("LOCAL_QUEUE_ALERT_CMD", "agents-notify").strip()
    if not cmd:
        return
    try:
        subprocess.run([cmd, msg], timeout=30, check=False,
                       stdin=subprocess.DEVNULL,
                       capture_output=True)
    except Exception:
        log.exception("alert command failed; the dead row is still in the DB")


def serve() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    poll = localqueue._float_env("LOCAL_QUEUE_POLL_INTERVAL_S",
                                 localqueue.DEFAULT_POLL_INTERVAL_S)
    conn = localqueue._connect()
    localqueue.init_db(conn)
    log.info("queue worker up — db=%s max_attempts=%d",
             localqueue.db_path(), localqueue.max_attempts())

    last_purge = 0.0
    try:
        while not _stop:
            try:
                worked = localqueue.run_once(conn, on_dead=notify_dead)
            except Exception:
                # A bug here must not end the process — systemd would restart it
                # into the same bug and rate-limit itself into being down.
                log.exception("dispatch loop error; backing off")
                time.sleep(5.0)
                continue

            now = time.time()
            if now - last_purge > PURGE_INTERVAL_S:
                try:
                    dropped = localqueue.purge_done(conn)
                    if dropped:
                        log.info("purged %d completed task(s)", dropped)
                except Exception:
                    log.exception("purge failed (harmless)")
                last_purge = now

            if not worked:
                time.sleep(poll)
    finally:
        conn.close()
    log.info("queue worker stopped")
    return 0


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="dispatch one due task and exit")
    parser.add_argument("--stats", action="store_true",
                        help="print counts per state as JSON and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.stats:
        print(json.dumps(localqueue.stats(), indent=2))
        return 0
    if args.once:
        conn = localqueue._connect()
        localqueue.init_db(conn)
        try:
            return 0 if localqueue.run_once(conn, on_dead=notify_dead) else 1
        finally:
            conn.close()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
