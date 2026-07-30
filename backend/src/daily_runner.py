"""Guarded entry point for the daily job — the local replacement for the Cloud
Run Job `health-tracker-daily`.

`run_daily.main()` is **not safe to run twice concurrently**. `upsert_daily` is
read-modify-write against a grid snapshot, so two overlapping executions both fail
to find a new date and append it TWICE. On Cloud Run that was prevented by
`_trigger_daily_sync` asking the Jobs API whether an execution was already in
flight. There is no Jobs API here, so the guard moves into the process itself:

    an exclusive, non-blocking flock. Whoever gets it runs; everyone else logs
    and exits 0.

The lock — not systemd — is the guarantee, and that is deliberate. Two independent
things start this job (the 11:00 backstop timer and a weigh-in), plus the occasional
manual run while debugging. Routing only *some* of them through systemd's
"already active" check would leave the manual path able to race the timer, which is
precisely the collision that appends a day twice. One lock covers all three.

Exit 0 on "already running" is also deliberate: it is the normal, expected outcome
of two screenshots sent back to back, not a failure, and a non-zero exit would fire
the unit's OnFailure alert every time.

    python -m src.daily_runner          # what the systemd unit runs
"""
from __future__ import annotations

import fcntl
import logging
import os
import sys
from pathlib import Path
from typing import Optional, TextIO

log = logging.getLogger("daily-runner")


def lock_path() -> Path:
    """Where the lock lives. Under $XDG_RUNTIME_DIR when available (tmpfs, cleared
    on reboot, which is right for a lock) and $XDG_STATE_HOME otherwise."""
    raw = os.environ.get("DAILY_LOCK_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime) if runtime else Path(
        os.environ.get("XDG_STATE_HOME", "").strip() or "~/.local/state"
    ).expanduser()
    return base / "health-tracker-daily.lock"


def acquire(path: Optional[Path] = None) -> Optional[TextIO]:
    """Take the exclusive lock, or return None if another run holds it.

    The handle is returned rather than closed because **the lock lives as long as
    the file object does** — closing it (or letting it be garbage collected)
    releases the lock immediately, which would defeat the whole guard. Callers must
    hold the reference for the duration of the run.
    """
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    handle = acquire()
    if handle is None:
        # The normal case for two weigh-ins in a row, and for a weigh-in that
        # lands while the 11:00 backstop is still running.
        log.info("daily sync already running; not starting another")
        return 0

    try:
        from src import run_daily      # imported late: the lock comes first
        run_daily.main()
        return 0
    except Exception:
        log.exception("daily sync failed")
        return 1
    finally:
        # Explicit close = explicit release, rather than relying on interpreter
        # teardown ordering.
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
