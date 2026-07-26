"""Durable storage for the Coach feature.

Everything the coach produces — the day's cards, the weekly review, the food
taxonomy it has learned, the long-term memory, the chat threads — lives here as
small JSON blobs in Cloud Storage.

Why this module exists at all: the first cut of the coach cached its generated
output in `/tmp` on Cloud Run. That service scales to zero and runs more than one
instance, so the cache was empty on almost every read, and the app fell through to
a 5-45 s Gemini call *on the screen the user was looking at*. The cache has to
outlive the instance, or the whole "generate in the background, read instantly"
premise collapses. Cloud Storage is the smallest thing that does: strongly
consistent, already enabled on the project, no database to provision.

Two properties the callers depend on:

  * **A read never fails the request.** A missing blob, a malformed blob or an
    unreachable bucket all return the caller's default. The coach degrades to
    "nothing new yet", never to a 500 on the app's main screen.
  * **Read-modify-write is safe.** Threads and memory are appended to from a
    scheduled generation and from a live chat turn at the same time; `update_json`
    uses GCS generation preconditions so a concurrent write is retried against the
    new content instead of silently clobbering it.

With `COACH_BUCKET` unset (tests, local runs) the same API is served from a local
directory, so nothing here needs credentials to be exercised.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("coach_store")

# -- blob layout ---------------------------------------------------------------
# One prefix for the whole feature, so the bucket can hold other things later and
# a stray key can never collide with the app's data.
ROOT = "coach"

STATE = f"{ROOT}/state.json"
TAXONOMY = f"{ROOT}/taxonomy.json"
MEMORY = f"{ROOT}/memory.json"

# ONE rolling blob holds every live card, rather than one blob per day.
#
# Per-day blobs looked tidier but were wrong: cards have very different lifetimes —
# the Sunday review is valid for eight days, a meal suggestion for five hours — so a
# reader would have had to fetch nine days of blobs to be sure it had them all, and
# nine round trips is not the ~100 ms read the app's main screen depends on. Cards
# expire by their own `expires_at` at merge and read time, and the feed is capped, so
# this blob stays small on its own.
FEED = f"{ROOT}/feed/current.json"


def thread_path(thread_id: str) -> str:
    return f"{ROOT}/threads/{thread_id}.json"


# A thread id comes in from the app, so it is checked before it becomes a path —
# an id with a slash or a `..` in it must never be able to address another blob.
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")


def is_safe_id(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(str(value or ""))) and ".." not in str(value)


# -- backend selection ---------------------------------------------------------

def bucket_name() -> str:
    return os.environ.get("COACH_BUCKET", "").strip()


def local_dir() -> str:
    return os.environ.get("COACH_LOCAL_DIR", "/tmp/health-tracker-coach")


def using_gcs() -> bool:
    return bool(bucket_name())


@functools.lru_cache(maxsize=1)
def _bucket():
    """The GCS bucket handle, built once per instance. Lazily imported so this
    module stays importable in tests without the client library installed."""
    from google.cloud import storage  # noqa: PLC0415 — lazy on purpose
    return storage.Client().bucket(bucket_name())


def _local_path(path: str) -> str:
    full = os.path.join(local_dir(), path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    return full


# -- primitives ----------------------------------------------------------------

def read_json(path: str, default: Any = None) -> Any:
    """The blob at `path` parsed as JSON, or `default` if it is missing, corrupt or
    unreachable. Never raises — every caller of this is on a read path the app is
    waiting on."""
    payload, _ = read_with_generation(path, default)
    return payload


def read_with_generation(path: str, default: Any = None):
    """As `read_json`, plus the blob's GCS generation (or None when it doesn't
    exist). The generation is what makes a later write conditional."""
    try:
        if using_gcs():
            blob = _bucket().blob(path)
            raw = blob.download_as_bytes()
            return json.loads(raw.decode("utf-8")), blob.generation
        with open(_local_path(path), "rb") as handle:
            return json.loads(handle.read().decode("utf-8")), os.path.getmtime(
                _local_path(path))
    except FileNotFoundError:
        return default, None
    except Exception as exc:  # missing blob, bad JSON, no credentials, network
        # NotFound is the common, boring case (nothing generated yet), so it is
        # logged at debug; anything else is worth seeing in the logs.
        if exc.__class__.__name__ == "NotFound":
            return default, None
        log.warning("coach read %s failed: %s", path, exc)
        return default, None


def write_json(path: str, payload: Any, *, if_generation_match: Any = None) -> bool:
    """Write `payload` as JSON. Returns True on success.

    `if_generation_match` makes the write conditional (0 means "only if the blob
    does not exist yet"); a precondition failure returns False rather than raising,
    so `update_json` can simply try again.
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        if using_gcs():
            blob = _bucket().blob(path)
            kwargs = {}
            if if_generation_match is not None:
                kwargs["if_generation_match"] = int(if_generation_match)
            blob.upload_from_string(data, content_type="application/json", **kwargs)
            return True
        # The local backend is for tests and one-off local runs; it has no
        # generation semantics, so the precondition is honoured only as
        # "exists / does not exist".
        full = _local_path(path)
        if if_generation_match == 0 and os.path.exists(full):
            return False
        tmp = f"{full}.tmp{os.getpid()}"
        with open(tmp, "wb") as handle:
            handle.write(data.encode("utf-8"))
        os.replace(tmp, full)
        return True
    except Exception as exc:
        if exc.__class__.__name__ == "PreconditionFailed":
            return False
        log.warning("coach write %s failed: %s", path, exc)
        return False


def update_json(path: str, mutate: Callable[[Any], Any], *, default: Any = None,
                attempts: int = 4) -> Any:
    """Read `path`, hand it to `mutate`, write the result back — atomically with
    respect to other writers.

    `mutate` must be pure enough to run more than once: on a lost race it is
    re-applied to the content that won. Returns the payload that was written, or
    the last attempt's payload if every write lost (which is logged — losing a
    memory fact is acceptable, losing it silently is not).
    """
    payload = None
    for attempt in range(attempts):
        current, generation = read_with_generation(path, default)
        payload = mutate(current)
        # A blob that doesn't exist yet is created with generation-0 ("only if
        # absent"), which is what makes two first-writers race safely.
        precondition = 0 if generation is None else generation
        if write_json(path, payload, if_generation_match=precondition):
            return payload
        if attempt < attempts - 1:
            time.sleep(0.1 * (attempt + 1) + random.random() * 0.05)
    log.warning("coach update %s lost %d races; last write dropped", path, attempts)
    return payload


def delete(path: str) -> bool:
    try:
        if using_gcs():
            _bucket().blob(path).delete()
            return True
        os.remove(_local_path(path))
        return True
    except Exception as exc:
        if exc.__class__.__name__ in ("NotFound", "FileNotFoundError"):
            return True
        log.warning("coach delete %s failed: %s", path, exc)
        return False


def list_names(prefix: str) -> List[str]:
    """Blob names under `prefix`, sorted. Used to find the most recent feed day
    when the app asks for "whatever is newest"."""
    try:
        if using_gcs():
            from google.cloud import storage  # noqa: PLC0415
            client = storage.Client()
            return sorted(b.name for b in client.list_blobs(bucket_name(),
                                                            prefix=prefix))
        base = os.path.join(local_dir(), prefix)
        directory = os.path.dirname(base)
        if not os.path.isdir(directory):
            return []
        out = []
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, local_dir())
            if rel.startswith(prefix) and os.path.isfile(full):
                out.append(rel)
        return sorted(out)
    except Exception as exc:
        log.warning("coach list %s failed: %s", prefix, exc)
        return []


# -- generation bookkeeping ----------------------------------------------------
# `state.json` is the coach's own scratchpad: when each slot last ran, which
# pattern findings have already been shown (so the feed doesn't repeat itself),
# and whether a generation is in flight (so the app can show a progress bar
# instead of guessing).

def default_state() -> Dict[str, Any]:
    return {"runs": {}, "shown": {}, "job": None}


def read_state() -> Dict[str, Any]:
    state = read_json(STATE, default=None)
    if not isinstance(state, dict):
        return default_state()
    for key, value in default_state().items():
        state.setdefault(key, value)
    return state


def update_state(mutate: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    def apply(current: Any) -> Dict[str, Any]:
        state = current if isinstance(current, dict) else default_state()
        for key, value in default_state().items():
            state.setdefault(key, value)

        return mutate(state)
    return update_json(STATE, apply, default=default_state())


def claim_job(job_id: str, reason: str, now_iso: str, *,
              stale_after_s: int = 240) -> Optional[Dict[str, Any]]:
    """Mark a generation as in flight, unless one already is.

    Returns the claimed job, or None if another generation started recently — that
    None is what stops a foregrounding app from firing three overlapping Gemini
    runs. A job older than `stale_after_s` is treated as dead (the instance that
    owned it was probably reaped mid-run) and can be taken over, so a crash can
    never wedge the feature permanently.
    """
    outcome: Dict[str, Any] = {}

    def apply(state: Dict[str, Any]) -> Dict[str, Any]:
        job = state.get("job") or None
        if isinstance(job, dict) and not job.get("finished_at"):
            age = _age_seconds(job.get("started_at"), now_iso)
            if age is not None and age < stale_after_s:
                outcome["claimed"] = None
                return state
        claimed = {"id": job_id, "reason": reason, "started_at": now_iso,
                   "finished_at": None}
        state["job"] = claimed
        outcome["claimed"] = claimed
        return state

    update_state(apply)
    return outcome.get("claimed")


def job_is_live(state: Dict[str, Any], now_iso: str, *,
                stale_after_s: int = 240) -> bool:
    """Whether a generation is actually running right now — what the app turns into
    a progress indicator. A job whose owner died is not live, however unfinished it
    looks, or the app would spin forever on a run that will never land."""
    job = (state or {}).get("job")
    if not isinstance(job, dict) or job.get("finished_at"):
        return False
    age = _age_seconds(job.get("started_at"), now_iso)
    return age is not None and age < stale_after_s


def finish_job(job_id: str, now_iso: str, *, error: Optional[str] = None) -> None:
    def apply(state: Dict[str, Any]) -> Dict[str, Any]:
        job = state.get("job")
        if isinstance(job, dict) and job.get("id") == job_id:
            job["finished_at"] = now_iso
            if error:
                job["error"] = error[:400]
            state["job"] = job
        return state
    update_state(apply)


def _age_seconds(then_iso: Any, now_iso: str) -> Optional[float]:
    from datetime import datetime
    try:
        then = datetime.fromisoformat(str(then_iso))
        now = datetime.fromisoformat(str(now_iso))
    except (TypeError, ValueError):
        return None
    if (then.tzinfo is None) != (now.tzinfo is None):
        then = then.replace(tzinfo=now.tzinfo)
    return (now - then).total_seconds()
