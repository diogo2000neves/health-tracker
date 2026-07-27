#!/usr/bin/env python3
"""The Coach's primary brain: Claude Sonnet, running on this Mac.

The backend does all the arithmetic and builds the prompt, then parks it as a job.
This worker claims a job, answers it with the local subscription `claude` CLI, and
posts the raw JSON back for the backend to validate and assemble. It holds no
nutrition logic at all — that is the point. Whichever model answers, the facts, the
prompt and the validation are identical; only the quality of the prose differs.

Why the local model at all, when the backend can call Gemini directly: the coaching
voice is the product. A flash model produces correct, forgettable sentences; Sonnet
notices that the oats held the morning and that lunch was the third day running
without anything green. That difference is worth waiting for, so:

  * If the Mac is asleep, nothing claims the job. It waits.
  * If Claude's five-hour usage window is spent, the worker RELEASES the job rather
    than consuming it, and tries again on the next tick.
  * If a job goes unanswered past `COACH_SONNET_WAIT_HOURS`, the backend's `/coach/sweep`
    runs the same prompt through Gemini. Waiting is allowed; silence is not.

Run by launchd every few minutes (see com.dneves.coach-worker.plist). Safe to run by
hand: `python3 automation/coach/worker.py --once`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# The audit pipeline's CLI wrapper already solves headless `claude` invocation and
# the robust JSON extraction that took several production failures to get right.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nutrition-audit"))
import claude_cli  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "worker.log"),
              logging.StreamHandler()],
)
log = logging.getLogger("coach-worker")

BACKEND = os.environ.get("HEALTH_BACKEND_URL",
                         "https://health-tracker-ingest-myznjtlyrq-ew.a.run.app")
TOKEN = os.environ.get("INGEST_TOKEN", "")
MODEL = os.environ.get("COACH_MODEL", "claude-sonnet-5")
EFFORT = os.environ.get("COACH_EFFORT", "high")
TIMEOUT_S = int(os.environ.get("COACH_TIMEOUT_S", "300"))
# Reports think for longer: a bigger model, a slower effort, and a prompt carrying a
# whole week of meals and advice.
REPORT_TIMEOUT_S = int(os.environ.get("COACH_REPORT_TIMEOUT_S", "900"))
WORKER_NAME = os.environ.get("COACH_WORKER_NAME", "mac")

# What a spent usage window looks like coming out of the CLI. Matching on the message
# is unlovely, but the distinction is essential: a usage limit means "come back
# later" (release the job, let it keep waiting for Sonnet) while a genuine error
# means this job is not going to work here.
_USAGE_LIMIT_MARKERS = (
    "usage limit", "rate limit", "limit reached", "quota", "429",
    "please wait", "try again later", "overloaded",
)


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None):
    url = f"{BACKEND.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Auth-Token": TOKEN,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
        if resp.status == 204 or not body.strip():
            return None
        return json.loads(body)


def claim() -> Optional[Dict[str, Any]]:
    try:
        return _request("GET", f"/coach/work?worker={WORKER_NAME}")
    except urllib.error.HTTPError as exc:
        # Collapse the body to one line: a Flask 404 is several lines of HTML, and a
        # log the user reads to see whether the coach is alive shouldn't be a page of
        # markup per poll.
        detail = " ".join(exc.read().decode(errors="replace").split())[:160]
        log.error("claim failed: HTTP %s %s", exc.code, detail)
    except Exception as exc:
        # No network, backend cold, laptop just woke up — all routine.
        log.info("claim unavailable: %s", exc)
    return None


def answer(job: Dict[str, Any]) -> Dict[str, Any]:
    """Run the job's prompt through the model the job asks for.

    The daily feed runs on Sonnet: frequent, fast, and good enough to notice that the
    oats held the morning. A weekly, monthly or yearly review asks for Opus at a
    slower effort, because correlating what was advised against what was eaten across
    weeks is a genuinely harder problem and happens rarely enough to afford it. A chat
    turn is Sonnet again but at medium effort — two grounded sentences, not a week of
    correlation. The job carries its own choice so this worker never has to know which
    is which.
    """
    model = job.get("model") or MODEL
    effort = job.get("effort") or EFFORT
    # The job's own budget when it states one. The `model`-implies-report fallback is
    # kept for jobs queued before that field existed, but it is a guess: chat also
    # names a model and must not wait fifteen minutes for two sentences.
    timeout = int(job.get("timeout_s")
                  or (REPORT_TIMEOUT_S if job.get("model") else TIMEOUT_S))
    log.info("running job %s (%s, %d chars) through %s at %s effort",
             job["id"], job.get("slot"), len(job["prompt"]), model, effort)
    started = time.monotonic()
    # No tools at all. This is pure reasoning over facts that are already in the
    # prompt, and a model that *can* write a file may decide to answer by writing
    # one: the first live run did exactly that — 238 s of good work, saved to disk,
    # with a prose summary where the JSON should have been.
    result = claude_cli.call_claude_json(
        job["prompt"], model=model, effort=effort, timeout_s=timeout,
        require_key=job.get("require_key", "cards"), tools="")
    log.info("job %s answered in %.1fs (cost %s)", job["id"],
             time.monotonic() - started, result.get("_cost_usd"))
    return result


def is_usage_limit(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _USAGE_LIMIT_MARKERS)


def run_once() -> bool:
    """Claim and answer at most one job. Returns True if one was handled."""
    job = claim()
    if not job:
        return False

    try:
        result = answer(job)
    except claude_cli.ClaudeError as exc:
        if is_usage_limit(exc):
            # Do NOT consume the job: it should keep waiting for Sonnet until the
            # backend decides the wait has gone on long enough.
            log.warning("usage limit — releasing job %s: %s", job["id"], exc)
            _request("POST", f"/coach/work/{job['id']}", {"release": str(exc)[:200]})
        else:
            log.error("job %s failed: %s", job["id"], exc)
            _request("POST", f"/coach/work/{job['id']}",
                     {"release": f"error: {str(exc)[:180]}"})
        return False

    model_id = result.pop("_model_id", job.get("model") or MODEL)
    result.pop("_cost_usd", None)
    applied = _request("POST", f"/coach/work/{job['id']}",
                       {"answer": result, "model": model_id})
    log.info("job %s applied: %s", job["id"], applied)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="handle at most one job and exit (the launchd mode)")
    parser.add_argument("--drain", action="store_true",
                        help="keep going until the queue is empty")
    args = parser.parse_args()

    if not TOKEN:
        log.error("INGEST_TOKEN is not set — cannot talk to the backend")
        return 2

    handled = 0
    while True:
        if not run_once():
            break
        handled += 1
        if not args.drain:
            break
    log.info("done: %d job(s) handled", handled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
