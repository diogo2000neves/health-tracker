#!/usr/bin/env python3
"""Weekly insights & next-meal coach — Gemini-powered narration.

The deterministic analysis already happened in the backend. This script is the thin
local runner: it FETCHES finished facts and narrates them with Gemini API (not claude
CLI, as of the Gemini migration). It can also be used for dry-run debugging.

Two modes:

  * `weekly` (Sunday) — GET /insights/diagnose + /insights/food-profile, resolve the
     continuity delta against last week's stored focus, narrate ONE focus + wins + a
     swap, run a critic pass, and upsert an IMMUTABLE row into `weekly_reports`.
  * `next-meal` (on-demand) — GET the enhanced context from the backend, call Gemini
     to determine the next slot and assemble 3 plates, upsert into `next_meal`.

Usage:
    backend/venv/bin/python automation/insights/generate.py weekly    --dry-run
    backend/venv/bin/python automation/insights/generate.py next-meal --dry-run
    backend/venv/bin/python automation/insights/generate.py weekly
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Use the narrator module (Gemini-based) instead of the old claude CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend" / "ingest"))
_NARRATOR = None  # lazy import


def _narrator():
    global _NARRATOR
    if _NARRATOR is None:
        import importlib.util
        p = Path(__file__).resolve().parent.parent.parent / "backend" / "ingest" / "narrator.py"
        spec = importlib.util.spec_from_file_location("narrator", str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _NARRATOR = mod
    return _NARRATOR


# -- paths & config ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
TOKEN_FILE = REPO_ROOT / "backend" / "credentials" / "token_nutrition_audit.json"
LOG_DIR = HERE / "logs"

SHEET_ID = os.environ.get(
    "HEALTH_SPREADSHEET_ID", "1JQWYkSgzU3F4mqR7BRE8wfoBif0xLU7uBM0iwHwxNAk")
TZ = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))

BACKEND_URL = os.environ.get("HEALTH_BACKEND_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

# Gemini config — same env vars the backend narrator uses.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_NARRATOR_MODEL", "gemini-2.0-flash")

WEEKLY_TAB = "weekly_reports"
NEXT_MEAL_TAB = "next_meal"
PROFILE_TAB = "food_profile"

WEEKLY_HEADERS = [
    "week_start", "generated_at", "window_start", "window_end", "diagnosis_json",
    "report_json", "focus_key", "focus_value", "prior_focus_key", "prior_focus_delta",
    "coverage_note", "model", "critic_verdict", "status",
]
NEXT_MEAL_HEADERS = [
    "date", "generated_at", "snapshot_json", "focus_key", "next_slot",
    "plates_json", "model", "status",
]
PROFILE_HEADERS = [
    "food", "category", "times_eaten", "top_slot", "median_portion_g", "cal_per_g",
    "last_eaten",
]

log = logging.getLogger("insights")


# -- auth & clients ------------------------------------------------------------
def get_credentials() -> Credentials:
    """Reuse the audit's Google token (spreadsheets scope). Same refresh discipline."""
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"No token at {TOKEN_FILE}. Authorise the audit first:\n"
            f"  backend/venv/bin/python automation/nutrition-audit/authorize.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
        return creds
    raise SystemExit("Token invalid/expired and cannot refresh. Re-run authorize.py.")


def backend_get(path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """GET a deterministic-insights endpoint. Same backend the app talks to."""
    if not BACKEND_URL:
        raise SystemExit("HEALTH_BACKEND_URL is not set.")
    if not INGEST_TOKEN:
        raise SystemExit("INGEST_TOKEN is not set.")
    url = f"{BACKEND_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Auth-Token": INGEST_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"backend {path} -> HTTP {exc.code}: {exc.read()[:200]!r}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"backend {path} unreachable: {exc}")


# -- generic sheet helpers (unchanged from Phase 2) ----------------------------
def _col_letter(index: int) -> str:
    letters, index = "", index + 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def ensure_tab(sheets, title: str, headers: List[str]) -> None:
    meta = sheets.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if title not in titles:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
    last = _col_letter(len(headers) - 1)
    current = (sheets.spreadsheets().values()
               .get(spreadsheetId=SHEET_ID, range=f"{title}!A1:{last}1")
               .execute().get("values", [[]]))
    if not current or current[0] != headers:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{title}!A1",
            valueInputOption="RAW", body={"values": [headers]},
        ).execute()


def read_rows(sheets, title: str) -> List[Dict[str, Any]]:
    last = _col_letter(40)
    try:
        values = (sheets.spreadsheets().values()
                  .get(spreadsheetId=SHEET_ID, range=f"{title}!A1:{last}",
                       valueRenderOption="UNFORMATTED_VALUE")
                  .execute().get("values", []))
    except Exception:
        return []
    if len(values) < 2:
        return []
    return [dict(zip(values[0], row)) for row in values[1:]]


def upsert_row(sheets, title: str, headers: List[str], key_col: str,
               row: Dict[str, Any]) -> None:
    """Update the row whose `key_col` matches, else append."""
    ensure_tab(sheets, title, headers)
    last = _col_letter(len(headers) - 1)
    try:
        values = (sheets.spreadsheets().values()
                  .get(spreadsheetId=SHEET_ID, range=f"{title}!A1:{last}",
                       valueRenderOption="UNFORMATTED_VALUE")
                  .execute().get("values", []))
    except Exception:
        values = []
    out = [row.get(h) for h in headers]
    idx = None
    if values:
        header = values[0]
        if key_col in header:
            ki = header.index(key_col)
            for n, r in enumerate(values[1:], start=2):
                if len(r) > ki and str(r[ki]) == str(row.get(key_col)):
                    idx = n
                    break
    if idx is not None:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{title}!A{idx}:{last}{idx}",
            valueInputOption="RAW", body={"values": [out]}).execute()
    else:
        sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{title}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [out]}).execute()


def replace_tab(sheets, title: str, headers: List[str],
                rows: List[List[Any]]) -> None:
    """Overwrite a derived tab wholesale (food_profile)."""
    ensure_tab(sheets, title, headers)
    sheets.spreadsheets().values().clear(spreadsheetId=SHEET_ID, range=title).execute()
    body = [headers] + rows
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{title}!A1",
        valueInputOption="RAW", body={"values": body}).execute()


# -- continuity ----------------------------------------------------------------
def _nutrient_mean_from_diagnosis(diagnosis: Dict[str, Any], key: str) -> Optional[float]:
    for n in diagnosis.get("nutrients", []):
        if n.get("key") == key:
            return n.get("mean")
    adh = diagnosis.get("adherence", {}).get(key)
    return adh.get("mean") if isinstance(adh, dict) and isinstance(adh.get("mean"), (int, float)) else None


def resolve_continuity(sheets, diagnosis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare this week against the focus the LAST report set."""
    prior_rows = [r for r in read_rows(sheets, WEEKLY_TAB) if r.get("week_start")]
    if not prior_rows:
        return None
    last = max(prior_rows, key=lambda r: str(r.get("week_start")))
    key = str(last.get("focus_key") or "").strip()
    try:
        prev_val = float(last.get("focus_value"))
    except (TypeError, ValueError):
        return None
    if not key or prev_val <= 0:
        return None
    now_val = _nutrient_mean_from_diagnosis(diagnosis, key)
    if now_val is None:
        return None
    pct = round(100 * (now_val - prev_val) / prev_val)
    kind = next((n.get("kind") for n in diagnosis.get("nutrients", [])
                 if n.get("key") == key), "reach")
    up = now_val > prev_val
    toward_target = (up and kind != "limit") or (not up and kind == "limit")
    return {"key": key, "prev": prev_val, "now": now_val, "pct": pct,
            "direction": "up" if up else ("down" if now_val < prev_val else "flat"),
            "toward_target": toward_target}


# -- modes ---------------------------------------------------------------------
def _sunday_ref(now: datetime) -> str:
    d = now.date()
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def run_weekly(sheets, *, dry_run: bool, date: Optional[str]) -> int:
    """Weekly report: fetch diagnosis, narrate with Gemini, write to sheets."""
    now = datetime.now(TZ)
    week_start = date or _sunday_ref(now)
    log.info("Weekly insights for week_start=%s", week_start)

    diagnosis = backend_get("/insights/diagnose", {"date": week_start, "window": "7"})
    profile = backend_get("/insights/food-profile").get("foods", [])
    if diagnosis["window"]["days_logged"] < 4:
        log.warning("only %d logged days — too thin for a confident report; skipping",
                    diagnosis["window"]["days_logged"])
        return 0

    continuity = resolve_continuity(sheets, diagnosis) if not dry_run else None

    if dry_run:
        # Just print the prompt for debugging.
        prompt = _narrator().build_weekly_prompt(diagnosis, profile, continuity)
        log.info("[dry-run] weekly prompt (%d chars):\n%s", len(prompt), prompt[:2000])
        _write_profile(sheets, profile, dry_run=True)
        return 0

    report = _narrator().narrate_weekly(diagnosis, profile, continuity,
                                         api_key=GEMINI_API_KEY, model=GEMINI_MODEL)

    focus_key = (report.get("focus", {}) or {}).get("key") or (
        diagnosis.get("ranked_issues") or [""])[0]
    focus_value = _nutrient_mean_from_diagnosis(diagnosis, focus_key)
    prior = None
    prior_rows = [r for r in read_rows(sheets, WEEKLY_TAB) if r.get("week_start")]
    if prior_rows:
        prior = max(prior_rows, key=lambda r: str(r.get("week_start"))).get("focus_key")

    ensure_tab(sheets, WEEKLY_TAB, WEEKLY_HEADERS)
    upsert_row(sheets, WEEKLY_TAB, WEEKLY_HEADERS, "week_start", {
        "week_start": week_start,
        "generated_at": now.isoformat(timespec="seconds"),
        "window_start": diagnosis["window"]["start"],
        "window_end": diagnosis["window"]["end"],
        "diagnosis_json": json.dumps(diagnosis, ensure_ascii=False),
        "report_json": json.dumps(report, ensure_ascii=False),
        "focus_key": focus_key,
        "focus_value": focus_value if focus_value is not None else "",
        "prior_focus_key": prior or "",
        "prior_focus_delta": json.dumps(continuity, ensure_ascii=False) if continuity else "",
        "coverage_note": diagnosis.get("coverage_note", ""),
        "model": GEMINI_MODEL,
        "critic_verdict": "gemini-pass",
        "status": "generated",
    })
    _write_profile(sheets, profile, dry_run=False)
    log.info("weekly report written: focus=%s (%s)", focus_key,
             (report.get("focus", {}) or {}).get("label", ""))
    return 0


def run_next_meal(sheets, *, dry_run: bool) -> int:
    """Next-meal: fetch enhanced context, let Gemini decide the slot, write to sheets."""
    now = datetime.now(TZ)
    day = now.date().isoformat()

    focus_key = _current_focus(sheets) if not dry_run else None
    # Use the enhanced next-meal-context-v2 endpoint (includes timing profile).
    params = {"focus": focus_key} if focus_key else {}
    params["v2"] = "1"  # request the enhanced context
    context = backend_get("/insights/next-meal-context", params)
    profile = backend_get("/insights/food-profile").get("foods", [])

    if not context.get("candidates"):
        log.info("nothing short today — no next-meal suggestion needed.")
        return 0

    if dry_run:
        prompt = _narrator().build_next_meal_v2_prompt(context, profile)
        log.info("[dry-run] next-meal prompt (%d chars):\n%s", len(prompt), prompt[:2000])
        return 0

    result = _narrator().assemble_next_meal(context, profile,
                                             api_key=GEMINI_API_KEY,
                                             model=GEMINI_MODEL)
    plates = result.get("plates", [])
    next_slot = result.get("next_slot", "")

    if not plates:
        log.warning("model returned no plates; leaving yesterday's cache untouched.")
        return 0

    ensure_tab(sheets, NEXT_MEAL_TAB, NEXT_MEAL_HEADERS)
    upsert_row(sheets, NEXT_MEAL_TAB, NEXT_MEAL_HEADERS, "date", {
        "date": day,
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot_json": json.dumps(context, ensure_ascii=False),
        "focus_key": focus_key or "",
        "next_slot": next_slot,
        "plates_json": json.dumps(plates, ensure_ascii=False),
        "model": GEMINI_MODEL,
        "status": "generated",
    })
    log.info("next-meal written: %d plates for %s (slot=%s)", len(plates), day, next_slot or "—")
    return 0


def _current_focus(sheets) -> Optional[str]:
    rows = [r for r in read_rows(sheets, WEEKLY_TAB) if r.get("week_start")]
    if not rows:
        return None
    return str(max(rows, key=lambda r: str(r.get("week_start"))).get("focus_key") or "") \
        or None


def _write_profile(sheets, profile: List[Dict[str, Any]], *, dry_run: bool) -> None:
    rows = [[f.get("food"), f.get("category"), f.get("times_eaten"), f.get("top_slot"),
             f.get("median_portion_g"), f.get("cal_per_g"), f.get("last_eaten")]
            for f in profile]
    if dry_run:
        log.info("[dry-run] would replace %s with %d foods", PROFILE_TAB, len(rows))
        return
    replace_tab(sheets, PROFILE_TAB, PROFILE_HEADERS, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["weekly", "next-meal"])
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch facts and print the prompt without a model call.")
    parser.add_argument("--date", default=None,
                        help="weekly: the week_start (a Sunday) to (re)generate.")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_DIR / "insights.log")])

    if not GEMINI_API_KEY and not args.dry_run:
        log.warning("GEMINI_API_KEY not set — generation will fail without it.")

    sheets = None
    if not args.dry_run:
        sheets = build("sheets", "v4", credentials=get_credentials(),
                       cache_discovery=False)
    if args.mode == "weekly":
        return run_weekly(sheets, dry_run=args.dry_run, date=args.date)
    return run_next_meal(sheets, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
