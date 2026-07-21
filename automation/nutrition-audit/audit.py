#!/usr/bin/env python3
"""Daily multi-model nutrition audit (runs locally on the MacBook).

The cloud ingest service analyses each meal photo ONCE with Gemini and writes the
result to the `meals` tab. This job upgrades that estimate with a local pipeline that
treats meal analysis as TWO problems:

  * PERCEPTION (what is on the plate, how many grams, hidden fats) — where different
    models genuinely disagree, so we ENSEMBLE: take Gemini's estimate (already in the
    row) plus a fresh, independent Claude estimate, and RECONCILE them against the
    image (adjudicate.py) instead of letting one overwrite the other. Disagreement is
    kept as signal, not discarded.
  * KNOWLEDGE (the ~30 micronutrient values for a known food × grams) — a lookup, not
    a guess, so we GROUND it in USDA FoodData Central (ground.py): measured, consistent,
    comparable across days; the model's estimate is kept only for the few keys FDC lacks.

Pipeline per meal:  Gemini(row) + Claude estimate  ->  adjudicate  ->  ground  ->  write.
A third estimator (e.g. Gemini 3.1 Pro) can plug in as a disagreement-gated tie-break
(see _THIRD_ESTIMATOR).

Design invariants carried over from the original single-model job:
  * LOCAL, not cloud — the subscription-backed `claude` CLI only exists on this Mac.
  * Idempotent — a meal is audited ONCE (the `model` column is stamped AUDIT_TAG and
    audited rows are skipped; a re-sent photo resets it and is re-audited).
  * Safe by construction — templates / text-only meals / stubs are skipped; row totals
    are re-summed from items in code; the target row is re-located by image_sha right
    before writing; and ANY stage failure degrades gracefully (adjudication failure ->
    the independent Claude estimate; grounding failure -> the model's own micros) so a
    real meal is never zeroed and a bad call never corrupts data.
  * No backend code is touched — only the shared Sheet is read and written.

Usage (backend/venv has the Google libs):
    backend/venv/bin/python automation/nutrition-audit/audit.py --check
    backend/venv/bin/python automation/nutrition-audit/audit.py --dry-run [--date YYYY-MM-DD] [--limit N]
    backend/venv/bin/python automation/nutrition-audit/audit.py            # live write
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import adjudicate
import claude_cli
import estimate as estimate_mod
import fdc
import ground as ground_mod
import nutrients
from nutrients import NON_MEALS, meal_totals, normalize_items, nutrient_key_count

# -- paths & config ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
TOKEN_FILE = REPO_ROOT / "backend" / "credentials" / "token_nutrition_audit.json"
LOG_DIR = HERE / "logs"
TMP_DIR = LOG_DIR / "tmp"

# The Sheet ID is not a secret (it lives in backend/CONTEXT.md); default it so the
# launchd job needs no environment, but allow the same override the backend uses.
SHEET_ID = os.environ.get(
    "HEALTH_SPREADSHEET_ID", "1JQWYkSgzU3F4mqR7BRE8wfoBif0xLU7uBM0iwHwxNAk")
TZ = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))

# The marker written into the `model` column once a row has been audited; selection
# skips any row whose `model` already starts with it, making re-runs idempotent.
AUDIT_TAG = "claude-audit"

MEALS_TAB = "meals"
# Column order is identical to the ingest service's MEALS_HEADERS — we write the same
# shape back. Indices used below are derived from this list, not hard-coded.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note", "template",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "N"
SHA_IDX = MEALS_HEADERS.index("image_sha")

# A tab the audit job OWNS entirely (the cloud services never read or write it). One
# row per reviewed meal, upserted on image_sha, laid out LEFT-TO-RIGHT as the story of
# the review: when it ran and how long it took, which models took part, what EACH model
# concluded on its own, how much they disagreed, what the adjudicator decided, where the
# micronutrients came from, and the final verdict. Built for transparency while testing.
REVIEWS_TAB = "meal_reviews"
REVIEWS_HEADERS = [
    "reviewed_at",          # local time the review ran
    "duration_s",           # how long this meal's pipeline took (seconds)
    "datetime",             # the meal's datetime — cross-references to the meals row
    "foods",                # the final food list
    "stage",                # adjudicated / single-estimate / fallback-estimate
    "models",               # which models took part + the adjudicator
    "gemini_said",          # Gemini's own conclusion (the ingest estimate in the row)
    "claude_said",          # the independent Claude estimate's conclusion
    "third_said",           # Gemini 3.1 Pro's conclusion, or why it wasn't invoked
    "disagreement",         # how far the estimates diverged — the ensemble signal
    "adjudicator_verdict",  # per-item: agreed / adjudicated / added, and why
    "grounding",            # per-item nutrient source: FDC entry vs kept-from-model
    "final",                # the final verdict: totals + confidence that got written
    "delta",                # before(Gemini)->after: calories, protein, nutrient-keys
    "review_notes",         # the adjudicator's own reasoning
    "image_sha",            # upsert key
]
REVIEWS_LAST_COL = chr(ord("A") + len(REVIEWS_HEADERS) - 1)

# ---- Phase 3 plug point: a disagreement-gated third estimator ----------------
# A callable (note: str, img_paths: List[Path]) -> estimate dict (same shape as
# estimate_mod.estimate). When set, it is called ONLY when Gemini and Claude diverge
# by more than THIRD_MODEL_DISAGREEMENT — spend the extra opinion where uncertainty
# actually is, not on every meal. Leave None until Gemini 3.1 Pro is wired in.
_THIRD_ESTIMATOR: Optional[Callable[[str, List[Path]], Dict[str, Any]]] = None
THIRD_MODEL_DISAGREEMENT = float(
    os.environ.get("AUDIT_THIRD_MODEL_DISAGREEMENT", "0.25"))

# The scopes the token MAY carry. Documentation only — the credential is loaded with
# the token's OWN granted scopes (see get_credentials).
KNOWN_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

log = logging.getLogger("nutrition-audit")

# Third estimator (a deliberate Gemini opinion) via the `agy` Antigravity CLI on the
# personal subscription — verified working headless. Wires in automatically when agy is
# installed; without it the pipeline is Gemini(row) + Claude, which is complete on its
# own. (The legacy `gemini` CLI is hard-blocked for individual tiers — IneligibleTierError
# "migrate to Antigravity" — which is why this goes through agy.)
try:
    import gemini_estimate
    if gemini_estimate.available():
        _THIRD_ESTIMATOR = gemini_estimate.estimate
    else:
        log.info("agy CLI not found — third estimator off (set AGY_BIN to enable)")
except Exception as _exc:  # noqa: BLE001
    log.warning("third estimator not wired: %s", _exc)


# -- auth & clients ------------------------------------------------------------
def get_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"No audit token at {TOKEN_FILE}.\n"
            f"Run: backend/venv/bin/python {HERE / 'authorize.py'}")
    # No explicit scopes: use the ones stored in the token file (the scopes actually
    # granted). Forcing a wider list here makes refresh request an ungranted scope and
    # fail with `invalid_scope` once the ~1h access token expires.
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)
        return creds
    raise SystemExit(
        "Audit token invalid/expired and cannot refresh. Re-run authorize.py.")


# -- meals tab -----------------------------------------------------------------
def read_meal_rows(sheets) -> List[Dict[str, Any]]:
    """Every meals row as a header-keyed dict (UNFORMATTED_VALUE — the sheet's
    European locale renders decimals with commas, which would break float())."""
    values = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID, range=f"{MEALS_TAB}!A1:{LAST_COL}",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )
    if len(values) < 2:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


def locate_row_by_sha(sheets, image_sha: str) -> Optional[int]:
    """Fresh 1-based sheet row for the non-stub meal with this image hash, or None.
    Re-read immediately before writing: rows are appended and re-sorted by the
    ingest service, so an index from an earlier read may be stale."""
    values = (
        sheets.spreadsheets().values()
        .get(spreadsheetId=SHEET_ID, range=f"{MEALS_TAB}!A1:{LAST_COL}",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )
    if not values:
        return None
    header = values[0]
    try:
        sha_i = header.index("image_sha")
        foods_i = header.index("foods")
    except ValueError:
        return None
    for n, row in enumerate(values[1:], start=2):
        foods = str(row[foods_i] if len(row) > foods_i else "").strip().lower()
        if (len(row) > sha_i and str(row[sha_i]) == str(image_sha)
                and foods not in NON_MEALS):
            return n
    return None


def is_audited(row: Dict[str, Any]) -> bool:
    return str(row.get("model") or "").strip().startswith(AUDIT_TAG)


# -- meal_reviews tab (audit-owned log) ----------------------------------------
def ensure_reviews_tab(sheets) -> None:
    """Create the meal_reviews tab with its header if absent, and keep the header in
    sync with REVIEWS_HEADERS. If the header CHANGED (a schema upgrade), the old rows
    were written under a different column layout and would misalign under the new
    header — so the whole tab is cleared and rewritten. That is safe: this tab is
    audit-owned test/provenance data, and any meal can be regenerated with --force.
    Idempotent — safe to call before every write."""
    meta = sheets.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if REVIEWS_TAB not in titles:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": REVIEWS_TAB}}}]},
        ).execute()
    current = (sheets.spreadsheets().values()
               .get(spreadsheetId=SHEET_ID,
                    range=f"{REVIEWS_TAB}!A1:{REVIEWS_LAST_COL}1")
               .execute().get("values", [[]]))
    header = current[0] if current else []
    if header == REVIEWS_HEADERS:
        return
    if header:  # a real, different header exists -> old layout, clear stale rows
        log.warning("meal_reviews schema changed — clearing old rows and rewriting "
                    "header (audit-owned data; re-run with --force to repopulate).")
        sheets.spreadsheets().values().clear(
            spreadsheetId=SHEET_ID, range=REVIEWS_TAB, body={}).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{REVIEWS_TAB}!A1",
        valueInputOption="RAW", body={"values": [REVIEWS_HEADERS]},
    ).execute()


def write_review(sheets, review: Dict[str, Any]) -> None:
    """Upsert one review row keyed on image_sha (append if new). Best-effort — the
    meal row is the record of truth; a failure here must not undo that write."""
    ensure_reviews_tab(sheets)
    values = (sheets.spreadsheets().values()
              .get(spreadsheetId=SHEET_ID,
                   range=f"{REVIEWS_TAB}!A1:{REVIEWS_LAST_COL}",
                   valueRenderOption="UNFORMATTED_VALUE")
              .execute().get("values", []))
    row = [review.get(h) for h in REVIEWS_HEADERS]
    idx = None
    if values:
        header = values[0]
        if "image_sha" in header:
            si = header.index("image_sha")
            for n, r in enumerate(values[1:], start=2):
                if len(r) > si and str(r[si]) == str(review.get("image_sha")):
                    idx = n
                    break
    if idx is not None:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{REVIEWS_TAB}!A{idx}:{REVIEWS_LAST_COL}{idx}",
            valueInputOption="RAW", body={"values": [row]},
        ).execute()
    else:
        sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{REVIEWS_TAB}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()


def select_meals(rows: List[Dict[str, Any]], day: str,
                 force: bool = False) -> List[Dict[str, Any]]:
    """Photo meals logged on `day` (local civil date) that still need auditing:
    have a photo, are a real meal (parseable non-empty items, not a stub), are not a
    measured template, and have not been audited yet (unless `force`)."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        dt = str(row.get("datetime") or "").strip()
        if not dt:
            continue
        try:
            row_day = datetime.fromisoformat(dt).astimezone(TZ).date().isoformat()
        except ValueError:
            row_day = dt[:10]
        if row_day != day:
            continue
        if not str(row.get("photo_url") or "").strip():
            continue                                   # text-only meal — nothing to see
        if str(row.get("template") or "").strip():
            continue                                   # measured template — ground truth
        if str(row.get("foods") or "").strip().lower() in NON_MEALS:
            continue                                   # stub
        if is_audited(row) and not force:
            continue                                   # already done
        try:
            items = json.loads(row.get("items") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(items, list) or not items:
            continue
        out.append(row)
    return out


# -- Drive ---------------------------------------------------------------------
_FILE_ID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)")


def photo_file_ids(photo_url: str) -> List[str]:
    """Drive file ids from a (space-joined) list of webViewLinks."""
    return _FILE_ID_RE.findall(str(photo_url or ""))


def _ext_for(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    # iPhone photos are HEIC: ISO-media 'ftyp' box with a heic/heif/mif1 brand. The
    # claude CLI's Read tool opens these natively (verified).
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1",
                                               b"heim", b"heis", b"hevm", b"hevs"):
        return "heic"
    return "jpg"


def download_photos(drive, file_ids: List[str], stem: str) -> List[Path]:
    """Fetch each Drive file by id and write it to a temp file; return the paths."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for i, fid in enumerate(file_ids, start=1):
        data = drive.files().get_media(fileId=fid).execute()
        if not data:
            continue
        path = TMP_DIR / f"{stem}_{i}.{_ext_for(data)}"
        path.write_bytes(data)
        paths.append(path)
    return paths


# -- the ensemble --------------------------------------------------------------
def _estimate_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap the estimate ALREADY in the meal row (Gemini's ingest output, pre-audit)
    as the first independent opinion — free, no API call. On a re-audit the row may
    hold a previous audit instead; either way it is a valid prior to reconcile."""
    try:
        items = normalize_items(json.loads(row.get("items") or "[]"))
    except (json.JSONDecodeError, TypeError):
        items = []
    return {
        "items": items,
        "confidence": nutrients._round_num(row.get("confidence"), 2),
        "_model_id": str(row.get("model") or "gemini").strip() or "gemini",
        "_source": "row",
    }


def _disagreement(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Relative divergence between two estimates on the numbers that matter. This is
    the ensemble's most informative output — a big gap means the meal is genuinely
    uncertain (and, for protein, matters directly to the recomp goal)."""
    ta, tb = meal_totals(a.get("items", [])), meal_totals(b.get("items", []))

    def rel(k: str) -> Optional[float]:
        x, y = ta.get(k, 0.0), tb.get(k, 0.0)
        denom = max(x, y)
        return round(abs(x - y) / denom, 2) if denom > 0 else None

    parts = {k: rel(k) for k in ("calories", "protein_g", "portion_g")}
    measurable = [v for v in parts.values() if v is not None]
    parts["max_rel"] = max(measurable) if measurable else 0.0
    return parts


def gather_estimates(row: Dict[str, Any], note: str, img_paths: List[Path]
                     ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build the independent estimates for one meal: Gemini (from the row) + a fresh
    Claude estimate, plus a disagreement-gated third estimator if one is wired in.
    Raises claude_cli.ClaudeError if the Claude estimate itself fails."""
    gemini = _estimate_from_row(row)
    claude = estimate_mod.estimate(note, img_paths)
    claude["_source"] = "claude"
    estimates = [gemini, claude]
    dis = _disagreement(gemini, claude) if gemini["items"] else {"max_rel": 0.0}

    if _THIRD_ESTIMATOR and dis.get("max_rel", 0.0) >= THIRD_MODEL_DISAGREEMENT:
        try:
            third = _THIRD_ESTIMATOR(note, img_paths)
            third["_source"] = "third"
            estimates.append(third)
            log.info("    third estimator invoked (disagreement %.0f%%)",
                     100 * dis["max_rel"])
        except Exception as exc:  # noqa: BLE001 — a third opinion is a bonus, never fatal
            log.warning("    third estimator failed (non-fatal): %s", exc)
    return estimates, dis


def _final_confidence(base: float, dis: Dict[str, Any]) -> float:
    """Lower the adjudicator's confidence when the independent estimates disagreed —
    Phase 4: make the disagreement visible through the existing confidence surface the
    app already shows, not just in a log."""
    penalty = min(0.35, 0.7 * float(dis.get("max_rel", 0.0)))
    return max(0.1, round(base - penalty, 2))


# -- one meal ------------------------------------------------------------------
def audit_meal(sheets, drive, row: Dict[str, Any], *, dry_run: bool) -> Optional[str]:
    """Run the full pipeline for one meal; write the revised row unless dry_run.
    Returns a one-line summary, or None if the meal was skipped (any failure leaves
    the original row untouched)."""
    t_start = time.monotonic()
    image_sha = str(row.get("image_sha") or "").strip()
    dt = str(row.get("datetime") or "")
    file_ids = photo_file_ids(row.get("photo_url"))
    if not file_ids:
        log.warning("  skip %s: no drive file id in photo_url", dt)
        _write_skip_review(sheets, row, t_start=t_start,
                           reason="no Drive photo id in photo_url", dry_run=dry_run)
        return None

    stem = re.sub(r"[^0-9]", "", dt)[:14] or "meal"
    try:
        img_paths = download_photos(drive, file_ids, stem)
    except Exception as exc:  # noqa: BLE001 — never let one meal kill the run
        log.warning("  skip %s: photo download failed: %s", dt, exc)
        _write_skip_review(sheets, row, t_start=t_start,
                           reason=f"photo download failed: {exc}", dry_run=dry_run)
        return None
    if not img_paths:
        log.warning("  skip %s: no photos downloaded", dt)
        _write_skip_review(sheets, row, t_start=t_start,
                           reason="no photos downloaded", dry_run=dry_run)
        return None

    note = str(row.get("note") or "")
    gemini_est = _estimate_from_row(row)   # for the before/after delta (pre-audit)
    try:
        estimates, dis = gather_estimates(row, note, img_paths)
    except claude_cli.ClaudeError as exc:
        log.warning("  skip %s: claude estimate failed: %s", dt, exc)
        for p in img_paths:                # clean up before bailing
            p.unlink(missing_ok=True)
        _write_skip_review(sheets, row, t_start=t_start,
                           reason=f"Claude estimate failed (likely usage cap): {exc}",
                           dry_run=dry_run)
        return None

    # STAGE 1 — adjudicate the independent estimates against the image. With only one
    # usable estimate (e.g. no Gemini row items), skip straight to grounding it.
    nonempty = [e for e in estimates if e.get("items")]
    adj_reasoning = ""
    resolutions: List[str] = []
    try:
        if len(nonempty) >= 2:
            adjudicated = adjudicate.adjudicate(note, img_paths, nonempty)
            final_items = adjudicated["items"]
            base_conf = adjudicated["confidence"]
            adj_model = adjudicated.get("_model_id", "claude")
            adj_reasoning = adjudicated.get("reasoning", "")
            stage = "adjudicated"
            resolutions = _resolution_summary(final_items)
        elif len(nonempty) == 1:
            final_items = nonempty[0]["items"]
            base_conf = nonempty[0].get("confidence", 0.5)
            adj_model = nonempty[0].get("_model_id", "claude")
            stage = "single-estimate"
        else:
            log.warning("  skip %s: no usable estimate", dt)
            _write_skip_review(sheets, row, t_start=t_start,
                               reason="no usable estimate from any model", dry_run=dry_run)
            return None
    except claude_cli.ClaudeError as exc:
        # Adjudication failed — fall back to the independent Claude estimate, i.e. the
        # original single-model behaviour. Grounding still runs on it.
        claude_est = next((e for e in estimates if e.get("_source") == "claude"), None)
        if not claude_est or not claude_est.get("items"):
            log.warning("  skip %s: adjudication failed and no fallback estimate: %s",
                        dt, exc)
            _write_skip_review(sheets, row, t_start=t_start,
                               reason=f"adjudication failed, no fallback estimate: {exc}",
                               dry_run=dry_run)
            return None
        log.warning("  %s: adjudication failed (%s) — using independent estimate", dt, exc)
        final_items = claude_est["items"]
        base_conf = claude_est.get("confidence", 0.5)
        adj_model = claude_est.get("_model_id", "claude")
        stage = "fallback-estimate"
    finally:
        for p in img_paths:                # done with images — don't leave photos on disk
            p.unlink(missing_ok=True)

    final_items = normalize_items(final_items)
    if not final_items:
        log.warning("  skip %s: pipeline returned no usable items (would zero a real "
                    "meal) — leaving original untouched", dt)
        _write_skip_review(sheets, row, t_start=t_start,
                           reason="pipeline returned no usable items", dry_run=dry_run)
        return None

    # STAGE 2 — ground the micronutrients in FDC (never raises; degrades to model).
    final_items, ground_report = ground_mod.ground(final_items)
    final_items = normalize_items(final_items)

    totals = meal_totals(final_items)
    confidence = _final_confidence(base_conf, dis)
    orig_model = str(row.get("model") or "").strip() or "unknown"
    new_model = f"{AUDIT_TAG}:{adj_model} | was:{orig_model}"

    # before/after against the pre-audit (Gemini) row.
    old_kcal = nutrients._round_num(row.get("calories"))
    old_prot = nutrients._round_num(row.get("protein_g"))
    old_nkeys = nutrient_key_count(gemini_est["items"])
    new_nkeys = nutrient_key_count(final_items)
    dis_str = _disagreement_str(dis)

    # The full per-model story for the meal_reviews row (and the dry-run log).
    models_str = _models_line(estimates, adj_model, stage)
    gemini_said = _estimate_summary(_by_source(estimates, "row"))
    claude_said = _estimate_summary(_by_source(estimates, "claude"))
    third_said = _third_said(estimates, dis)
    ground_detail = _grounding_detail(ground_report)
    if stage == "adjudicated":
        adj_verdict = "; ".join(resolutions) if resolutions else "all items agreed — no changes"
    elif stage == "single-estimate":
        adj_verdict = "single estimate — no reconciliation (only one usable estimate)"
    else:  # fallback-estimate
        adj_verdict = "adjudication FAILED → used the independent Claude estimate"
    final_str = (f"{totals['calories']:.0f} kcal | P{totals['protein_g']:.0f} "
                 f"C{totals['carbs_g']:.0f} F{totals['fat_g']:.0f} | "
                 f"{totals['portion_g']:.0f}g | {len(final_items)} items | "
                 f"{new_nkeys} nutr-keys | conf {confidence}")
    delta = (f"kcal {old_kcal:.0f}->{totals['calories']:.0f} | "
             f"protein {old_prot:.0f}->{totals['protein_g']:.0f}g | "
             f"nutrient-keys {old_nkeys}->{new_nkeys}")

    summary = (f"{dt} | {totals['foods'][:40]} | [{stage}] "
               f"kcal {old_kcal:.0f}->{totals['calories']:.0f} "
               f"protein {old_prot:.0f}->{totals['protein_g']:.0f}g "
               f"keys {old_nkeys}->{new_nkeys} disagree[{dis_str}] "
               f"conf {confidence} ({time.monotonic() - t_start:.0f}s)")

    if dry_run:
        log.info("  [dry-run] would update %s", summary)
        log.info("    models     : %s", models_str)
        log.info("    gemini said: %s", gemini_said)
        log.info("    claude said: %s", claude_said)
        log.info("    third  said: %s", third_said)
        log.info("    adjudicator: %s", adj_verdict)
        log.info("    grounding  : %s", ground_detail)
        log.info("    FINAL      : %s", final_str)
        if adj_reasoning:
            log.info("    reasoning  : %s", adj_reasoning[:280])
        return summary

    # Re-locate the row freshly right before writing — indices shift under the
    # concurrent ingest/daily writers.
    rownum = locate_row_by_sha(sheets, image_sha)
    if rownum is None:
        log.warning("  skip %s: row vanished/changed before write (sha %s)",
                    dt, image_sha[:12])
        return None

    new_row = {
        **row,
        **totals,
        "items": json.dumps(final_items, ensure_ascii=False),
        "confidence": confidence,
        "model": new_model,
    }
    values = [[new_row.get(h) for h in MEALS_HEADERS]]
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{MEALS_TAB}!A{rownum}:{LAST_COL}{rownum}",
        valueInputOption="RAW", body={"values": values},
    ).execute()
    log.info("  updated %s", summary)

    # Log the full review story (best-effort: the meal row above is the record of truth).
    try:
        write_review(sheets, {
            "reviewed_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - t_start, 1),
            "datetime": dt,
            "foods": totals["foods"],
            "stage": stage,
            "models": models_str,
            "gemini_said": gemini_said,
            "claude_said": claude_said,
            "third_said": third_said,
            "disagreement": dis_str,
            "adjudicator_verdict": adj_verdict[:900],
            "grounding": ground_detail,
            "final": final_str,
            "delta": delta,
            "review_notes": (adj_reasoning or "")[:2000],
            "image_sha": image_sha,
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("  meal updated, but review-log write failed (non-fatal): %s", exc)
    return summary


def _resolution_summary(items: List[Dict[str, Any]]) -> List[str]:
    """Compact per-item adjudicator outcomes for the review log."""
    out = []
    for it in items:
        res = it.get("_resolution")
        if not res:
            continue
        note = it.get("_resolution_note")
        out.append(f"{it['name']}: {res}" + (f" ({note})" if note else ""))
    return out


def _disagreement_str(dis: Dict[str, Any]) -> str:
    def pct(k: str) -> str:
        v = dis.get(k)
        return f"{k.split('_')[0]} {round(100 * v)}%" if v is not None else ""
    return " | ".join(p for p in (pct("calories"), pct("protein_g"),
                                  pct("portion_g")) if p) or "n/a"


# -- per-model transparency for the review log ---------------------------------
def _by_source(estimates: List[Dict[str, Any]], source: str) -> Optional[Dict[str, Any]]:
    return next((e for e in estimates if e.get("_source") == source), None)


def _estimate_summary(est: Optional[Dict[str, Any]]) -> str:
    """One model's own conclusion in a glance: macros, portion, item + nutrient counts."""
    if not est or not est.get("items"):
        return "—"
    t = meal_totals(est["items"])
    return (f"{t['calories']:.0f} kcal | P{t['protein_g']:.0f} C{t['carbs_g']:.0f} "
            f"F{t['fat_g']:.0f} | {t['portion_g']:.0f}g | {len(est['items'])} items | "
            f"{nutrient_key_count(est['items'])} nutr-keys")


def _models_line(estimates: List[Dict[str, Any]], adj_model: str, stage: str) -> str:
    parts = []
    for src, label in (("row", "gemini(row)"), ("claude", "claude"), ("third", "third")):
        e = _by_source(estimates, src)
        if e:
            parts.append(f"{label}={e.get('_model_id', '?')}")
    parts.append(f"adjudicator={adj_model}" if stage == "adjudicated"
                 else f"verdict={stage}")
    return " | ".join(parts)


def _third_said(estimates: List[Dict[str, Any]], dis: Dict[str, Any]) -> str:
    """The third model's conclusion, or a clear reason it didn't run — so a test can
    always tell whether Gemini 3.1 Pro was consulted and why."""
    third = _by_source(estimates, "third")
    if third:
        return _estimate_summary(third)
    if _THIRD_ESTIMATOR is None:
        return "not configured (gemini CLI not found)"
    mr = float(dis.get("max_rel", 0.0))
    return (f"not invoked — estimates agreed within gate "
            f"({mr * 100:.0f}% < {THIRD_MODEL_DISAGREEMENT * 100:.0f}%)")


def _grounding_detail(report: Dict[str, Any]) -> str:
    """Per-item nutrient provenance: which FDC entry backed it, or that the model's
    own estimate was kept (and why). This is the 'which model did what' for Layer B."""
    head = (f"{report['grounded']}/{report['total']} items FDC, "
            f"{report['keys_from_fdc']} keys")
    bits = []
    for d in report.get("detail", []):
        name = d.get("name", "?")
        if d.get("source") == "fdc":
            bits.append(f"{name}→fdc:{d.get('fdc_id')} ({d.get('keys_from_fdc')}k)")
        else:
            bits.append(f"{name}→model({d.get('reason', 'no_match')})")
    return (head + " | " + "; ".join(bits))[:900] if bits else head


def _write_skip_review(sheets, row: Dict[str, Any], *, t_start: float,
                       reason: str, dry_run: bool) -> None:
    """Record that a meal was NOT audited (its original estimate is kept) — so a capped
    or failed run shows up in meal_reviews instead of doing nothing silently. Upserted
    on image_sha, so a later successful audit overwrites this skip row. No-op in dry-run,
    and never clobbers an existing good audit (a --force re-audit that fails leaves the
    prior review intact)."""
    if dry_run or is_audited(row):
        return
    gem = _estimate_from_row(row)
    try:
        write_review(sheets, {
            "reviewed_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - t_start, 1),
            "datetime": str(row.get("datetime") or ""),
            "foods": str(row.get("foods") or ""),
            "stage": "skipped",
            "models": f"gemini(row)={gem['_model_id']} | audit not run",
            "gemini_said": _estimate_summary(gem),
            "claude_said": "—",
            "third_said": "—",
            "disagreement": "n/a",
            "adjudicator_verdict": "—",
            "grounding": "not grounded",
            "final": "KEPT ORIGINAL Gemini estimate — not audited",
            "delta": "unchanged",
            "review_notes": reason[:500],
            "image_sha": str(row.get("image_sha") or "").strip(),
        })
        log.info("  logged skip to meal_reviews (%s)", reason[:80])
    except Exception as exc:  # noqa: BLE001
        log.warning("  skip-review write failed (non-fatal): %s", exc)


# -- modes ---------------------------------------------------------------------
def run_check(sheets, drive) -> int:
    """Verify the pipeline's dependencies: read the meals tab, download one real
    photo, and reach FDC."""
    rows = read_meal_rows(sheets)
    log.info("Sheets read OK: %d meal rows.", len(rows))
    with_photo = [r for r in rows if photo_file_ids(r.get("photo_url"))]
    if with_photo:
        row = with_photo[-1]
        fids = photo_file_ids(row.get("photo_url"))
        try:
            paths = download_photos(drive, fids[:1], "check")
        except Exception as exc:  # noqa: BLE001
            log.error("Drive download FAILED with the current scope: %s", exc)
            log.error("If this is a 403/insufficient-scope, re-run:\n"
                      "  backend/venv/bin/python %s --drive-readonly",
                      HERE / "authorize.py")
            return 1
        if paths:
            log.info("Drive download OK: %s (%d bytes).", paths[0].name,
                     paths[0].stat().st_size)
            paths[0].unlink(missing_ok=True)
    else:
        log.warning("No meal rows with a Drive photo to test the download path.")

    try:
        cands = fdc.search("butter", page_size=1)
        fdc.flush_cache()
        log.info("FDC reach OK: 'butter' -> %s (key=%s).",
                 cands[0]["description"][:40] if cands else "no hit",
                 "custom" if fdc.API_KEY != "DEMO_KEY" else "DEMO_KEY (get a free key)")
    except Exception as exc:  # noqa: BLE001
        log.error("FDC reachability FAILED: %s", exc)
        return 1
    log.info("--check passed. Sheets + Drive + FDC all reachable.")
    return 0


def run_audit(sheets, drive, day: str, *, dry_run: bool, limit: Optional[int],
              force: bool = False) -> int:
    rows = read_meal_rows(sheets)
    selected = select_meals(rows, day, force=force)
    if limit is not None:
        selected = selected[:limit]
    mode = "DRY-RUN" if dry_run else ("LIVE-FORCE" if force else "LIVE")
    log.info("[%s] %s: %d meal(s) to audit on %s.", mode, day, len(selected), day)
    audited = 0
    for row in selected:
        log.info("Auditing: %s (%s)", str(row.get("foods"))[:60], row.get("datetime"))
        if audit_meal(sheets, drive, row, dry_run=dry_run) is not None:
            audited += 1
    log.info("[%s] done: %d/%d meal(s) %s.", mode, audited, len(selected),
             "would be updated" if dry_run else "updated")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Verify Sheets read + one Drive photo download + FDC reach.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to the Sheet.")
    parser.add_argument("--date", default=None,
                        help="Audit this local date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Audit at most N meals (useful for a first live test).")
    parser.add_argument("--force", action="store_true",
                        help="Re-review meals even if already audited.")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    claude_cli.set_debug_dir(TMP_DIR)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_DIR / "audit.log")],
    )

    creds = get_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    if args.check:
        return run_check(sheets, drive)

    day = args.date or datetime.now(TZ).date().isoformat()
    return run_audit(sheets, drive, day, dry_run=args.dry_run, limit=args.limit,
                     force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
