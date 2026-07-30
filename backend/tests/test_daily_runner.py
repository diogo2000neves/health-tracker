"""Unit tests for the daily job's concurrency guard.

The one thing that must hold: `run_daily.main()` never runs twice at once. It
upserts against a pre-read grid snapshot, so two overlapping executions both miss a
new date and append it TWICE — a corrupted day that then has to be healed by hand.
On Cloud Run the Jobs API enforced this; here an flock does.
"""
import multiprocessing
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import src  # noqa: E402
from src import daily_runner  # noqa: E402


def _stub_run_daily(monkeypatch, fn):
    """Replace `src.run_daily` for the duration of a test.

    `daily_runner.main` does `from src import run_daily`, which resolves through
    the PACKAGE ATTRIBUTE once any other test in the suite has imported the real
    module — patching only `sys.modules` passes in isolation and is silently
    ignored in a full run (it was, the first time). Patch both.
    """
    class Stub:
        main = staticmethod(fn)

    monkeypatch.setattr(src, "run_daily", Stub, raising=False)
    monkeypatch.setitem(sys.modules, "src.run_daily", Stub)
    return Stub


def test_lock_is_exclusive(tmp_path):
    lock = tmp_path / "daily.lock"
    first = daily_runner.acquire(lock)
    assert first is not None
    try:
        assert daily_runner.acquire(lock) is None, "second run must be turned away"
    finally:
        first.close()


def test_lock_is_released_on_close(tmp_path):
    """Releasing matters as much as taking: a lock never released would mean the
    weigh-in trigger silently stops working until the next reboot."""
    lock = tmp_path / "daily.lock"
    handle = daily_runner.acquire(lock)
    assert handle is not None
    handle.close()
    again = daily_runner.acquire(lock)
    assert again is not None
    again.close()


def test_lock_records_the_holding_pid(tmp_path):
    """So a stuck run can be identified without guessing."""
    import os
    lock = tmp_path / "daily.lock"
    handle = daily_runner.acquire(lock)
    try:
        assert lock.read_text().strip() == str(os.getpid())
    finally:
        handle.close()


def test_lock_path_honours_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_LOCK_FILE", str(tmp_path / "custom.lock"))
    assert daily_runner.lock_path() == tmp_path / "custom.lock"


def test_lock_path_prefers_runtime_dir(tmp_path, monkeypatch):
    """A lock belongs on tmpfs — cleared by a reboot, which is exactly the
    semantics wanted if the machine died mid-run."""
    monkeypatch.delenv("DAILY_LOCK_FILE", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert daily_runner.lock_path().parent == tmp_path


def test_main_skips_cleanly_when_already_running(tmp_path, monkeypatch):
    """Exit 0, not an error: two screenshots in a row is the normal case, and a
    non-zero exit would fire the systemd OnFailure alert every single time."""
    monkeypatch.setenv("DAILY_LOCK_FILE", str(tmp_path / "daily.lock"))
    ran = []
    _stub_run_daily(monkeypatch, lambda: ran.append("ran"))

    held = daily_runner.acquire()
    assert held is not None
    try:
        assert daily_runner.main() == 0
        assert ran == [], "the job must not have run while the lock was held"
    finally:
        held.close()


def test_main_runs_the_job_when_free(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILY_LOCK_FILE", str(tmp_path / "daily.lock"))
    calls = []
    _stub_run_daily(monkeypatch, lambda: calls.append("ran"))

    assert daily_runner.main() == 0
    assert calls == ["ran"]


def test_main_reports_failure_without_raising(tmp_path, monkeypatch):
    """A failing sync must exit non-zero (so OnFailure alerts) but must not
    traceback out of the unit."""
    monkeypatch.setenv("DAILY_LOCK_FILE", str(tmp_path / "daily.lock"))

    def explode():
        raise RuntimeError("sheets unreachable")

    _stub_run_daily(monkeypatch, explode)
    assert daily_runner.main() == 1


def test_lock_frees_after_the_run(tmp_path, monkeypatch):
    """The trigger fires several times a day; a leaked lock would break every
    subsequent one."""
    monkeypatch.setenv("DAILY_LOCK_FILE", str(tmp_path / "daily.lock"))
    _stub_run_daily(monkeypatch, lambda: None)
    assert daily_runner.main() == 0
    after = daily_runner.acquire()
    assert after is not None, "lock leaked after a completed run"
    after.close()


def _hold_lock(path, ready, release):
    """Child process: take the lock, signal, wait, exit."""
    handle = daily_runner.acquire(pathlib.Path(path))
    ready.set() if handle is not None else None
    release.wait(timeout=10)
    if handle:
        handle.close()


def test_lock_is_exclusive_across_processes(tmp_path):
    """flock is per-open-file-description, so the in-process test above could pass
    while cross-process locking was broken. The timer and the weigh-in trigger are
    genuinely different processes, so that is the case that matters."""
    lock = tmp_path / "daily.lock"
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(lock), ready, release))
    child.start()
    try:
        assert ready.wait(timeout=10), "child never acquired the lock"
        assert daily_runner.acquire(lock) is None, "lock did not cross processes"
    finally:
        release.set()
        child.join(timeout=10)
