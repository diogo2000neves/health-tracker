#!/usr/bin/env python3
"""Weekly insights & next-meal coach — the strong-model narration (runs on the Mac).

The deterministic analysis already happened in the backend. This job is the thin,
local half of the design: it FETCHES finished facts and turns them into a human
coaching voice with the strongest model available (the subscription `claude` CLI, the
same transport the meal audit uses). No arithmetic is done here — every number on
screen came from `ingest/insights.py`; the model only decides what to SAY.

Two modes, two launchd triggers:

  * `weekly`   (Sunday) — GET /insights/diagnose + /insights/food-profile, resolve the
     continuity delta against last week's stored focus, narrate ONE focus + wins + a
     swap, run a critic pass, and upsert an IMMUTABLE row into `weekly_reports`. Never
     rebuilt — continuity depends on last week's report being the exact snapshot the
     user read.
  * `next-meal` (daily, afternoon) — GET /insights/next-meal-context, assemble THREE
     ranked plates from foods the user already eats (portion ranges are the backend's,
     not the model's), and upsert today's row into `next_meal` (a disposable cache).

Design invariants carried from the audit:
  * LOCAL only — the subscription model lives on this Mac; if it's offline the app
    serves the last good report and we simply wait.
  * The model NEVER invents a number. Facts in, prose out; a critic rejects any claim
    the facts don't support, any alarm the policy says is benign, any restrictive
    framing.
  * Safe by construction — a parse/critic failure writes nothing (the previous report
    stands); `weekly_reports` is upserted on week_start so a re-run is idempotent.

Usage (backend/venv has the Google libs; reuses the audit's token + claude_cli):
    backend/venv/bin/python automation/insights/generate.py weekly    --dry-run
    backend/venv/bin/python automation/insights/generate.py next-meal --dry-run
    backend/venv/bin/python automation/insights/generate.py weekly            # live
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

# Reuse the audit's battle-tested headless-CLI wrapper rather than re-deriving the
# "strip the fence, survive trailing prose, fail closed" parsing it took production
# failures to get right.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "nutrition-audit"))
import claude_cli  # noqa: E402

# -- paths & config ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
TOKEN_FILE = REPO_ROOT / "backend" / "credentials" / "token_nutrition_audit.json"
LOG_DIR = HERE / "logs"

SHEET_ID = os.environ.get(
    "HEALTH_SPREADSHEET_ID", "1JQWYkSgzU3F4mqR7BRE8wfoBif0xLU7uBM0iwHwxNAk")
TZ = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))

# The backend that already did the deterministic analysis. The token is the same
# X-Auth-Token the app sends (INGEST_TOKEN); the base URL is the Cloud Run service.
BACKEND_URL = os.environ.get("HEALTH_BACKEND_URL", "").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")

# The strongest model the local subscription offers — quality of the advice is the
# whole point, so this is not the place to economise. Overridable.
MODEL = os.environ.get("INSIGHTS_MODEL", "claude-sonnet-5")
EFFORT = os.environ.get("INSIGHTS_EFFORT", "xhigh")
CRITIC_EFFORT = os.environ.get("INSIGHTS_CRITIC_EFFORT", "high")
CALL_TIMEOUT_S = int(os.environ.get("INSIGHTS_TIMEOUT_S", "900"))

WEEKLY_TAB = "weekly_reports"
NEXT_MEAL_TAB = "next_meal"
PROFILE_TAB = "food_profile"

WEEKLY_HEADERS = [
    "week_start", "generated_at", "window_start", "window_end", "diagnosis_json",
    "report_json", "focus_key", "focus_value", "prior_focus_key", "prior_focus_delta",
    "coverage_note", "model", "critic_verdict", "status",
]
NEXT_MEAL_HEADERS = [
    "date", "generated_at", "snapshot_json", "focus_key", "plates_json", "model",
    "status",
]
PROFILE_HEADERS = [
    "food", "category", "times_eaten", "top_slot", "median_portion_g", "cal_per_g",
    "last_eaten",
]

log = logging.getLogger("insights")


# -- auth & clients ------------------------------------------------------------
def get_credentials() -> Credentials:
    """Reuse the audit's Google token (spreadsheets scope). Same refresh discipline:
    load with the token's own granted scopes so the ~1h refresh never asks for one it
    wasn't granted."""
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
    """GET a deterministic-insights endpoint. Fails loudly — without the facts there is
    nothing to narrate, and a partial report is worse than none."""
    if not BACKEND_URL:
        raise SystemExit("HEALTH_BACKEND_URL is not set (the Cloud Run base URL).")
    if not INGEST_TOKEN:
        raise SystemExit("INGEST_TOKEN is not set (the app's X-Auth-Token).")
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


# -- generic sheet helpers -----------------------------------------------------
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
    values = (sheets.spreadsheets().values()
              .get(spreadsheetId=SHEET_ID, range=f"{title}!A1:{last}",
                   valueRenderOption="UNFORMATTED_VALUE")
              .execute().get("values", []))
    if len(values) < 2:
        return []
    return [dict(zip(values[0], row)) for row in values[1:]]


def upsert_row(sheets, title: str, headers: List[str], key_col: str,
               row: Dict[str, Any]) -> None:
    """Update the row whose `key_col` matches, else append. Keeps history for keyed
    tabs (weekly_reports on week_start, next_meal on date)."""
    last = _col_letter(len(headers) - 1)
    values = (sheets.spreadsheets().values()
              .get(spreadsheetId=SHEET_ID, range=f"{title}!A1:{last}",
                   valueRenderOption="UNFORMATTED_VALUE")
              .execute().get("values", []))
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
    """Overwrite a derived tab wholesale (food_profile) — pure function of meals,
    rebuilt each run, so a full replace can never leave stale foods behind."""
    ensure_tab(sheets, title, headers)
    sheets.spreadsheets().values().clear(spreadsheetId=SHEET_ID, range=title).execute()
    body = [headers] + rows
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID, range=f"{title}!A1",
        valueInputOption="RAW", body={"values": body}).execute()


# -- continuity ----------------------------------------------------------------
def _nutrient_mean(diagnosis: Dict[str, Any], key: str) -> Optional[float]:
    for n in diagnosis.get("nutrients", []):
        if n.get("key") == key:
            return n.get("mean")
    adh = diagnosis.get("adherence", {}).get(key)
    return adh.get("mean") if adh else None


def resolve_continuity(sheets, diagnosis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare this week against the focus the LAST report set — the fact that turns a
    report into a coach ('omega-3 up 40% since I flagged it'). Deterministic: the
    number is recomputed from this week's Diagnosis, never guessed."""
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
    now_val = _nutrient_mean(diagnosis, key)
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


# -- prompts -------------------------------------------------------------------
_COACH_RULES = """És um coach de nutrição atencioso e prático a falar com o utilizador
em português de Portugal (tratamento por "tu"). O objetivo dele é recomposição
corporal: perder gordura mantendo músculo, com proteína alta.

REGRAS ABSOLUTAS:
- Os FACTOS abaixo já foram calculados. NUNCA inventes nem recalcules um número; usa
  só os que te são dados. Se não há dado, não afirmes nada sobre isso.
- Escolhe UM único foco para a semana — o mais importante (já vem priorizado em
  `ranked_issues`). Nada de listas de 10 dicas; uma pessoa com 10 dicas faz zero.
- Celebra o que está a correr bem (`wins`) — o reforço é o que mantém um hábito.
- NÃO alarmes sobre o que a política diz ser benigno (ex.: colesterol alimentar, um
  excesso marcado como não-problema). Mede as palavras; sê calmo, não catastrófico.
- Sem linguagem médica nem diagnósticos. Se algo pede análises, sugere "vale a pena
  um exame", nunca um veredito.
- Enquadra a suficiência como vitória; nunca incentives comer menos por comer menos.
- Uma frase por campo. Concreto, caloroso, humano. Nada de jargão."""

_REPORT_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "headline": "uma frase que resume a semana",
  "wins": [{"title": "curto", "detail": "uma frase"}],
  "focus": {
    "key": "<a chave de ranked_issues[0]>",
    "label": "<nome do nutriente em pt-PT>",
    "why": "porque importa, numa frase, com o número relevante",
    "attribution": "de onde vem, se houver `attribution` (ex.: 68% vem do chouriço)",
    "severity": "high|medium|low"
  },
  "swap": {"from": "alimento a reduzir/atual", "to": "alternativa melhor e realista",
           "why": "uma frase — porquê"},
  "continuity": "uma frase sobre o progresso desde a última semana, OU null se não houver",
  "encouragement": "uma frase final, motivadora e humana"
}"""


def build_weekly_prompt(diagnosis: Dict[str, Any], profile: List[Dict[str, Any]],
                        continuity: Optional[Dict[str, Any]]) -> str:
    top_foods = [f["food"] for f in profile[:20]]
    facts = {
        "window": diagnosis.get("window"),
        "adherence": diagnosis.get("adherence"),
        "ranked_issues": diagnosis.get("ranked_issues"),
        "nutrients": [n for n in diagnosis.get("nutrients", [])
                      if n.get("genuine_issue") or n.get("status") in
                      ("over_benign", "approaching_ul") or n.get("key") in
                      diagnosis.get("ranked_issues", [])],
        "wins": diagnosis.get("wins"),
        "coverage_note": diagnosis.get("coverage_note"),
        "basis": diagnosis.get("basis"),
        "continuity": continuity,
        "foods_the_user_eats": top_foods,
    }
    return (f"{_COACH_RULES}\n\nFACTOS (JSON, já calculados):\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n{_REPORT_SCHEMA}")


_CRITIC_RULES = """És um revisor rigoroso. Recebes os FACTOS calculados e um RASCUNHO de
conselho. Verifica, sem simpatia:
1. Cada afirmação numérica do rascunho é suportada pelos factos? (nada inventado)
2. O nível de alarme condiz? Nada tratado como grave se a política o marca benigno.
3. Sem linguagem médica/diagnóstico. Sem incentivo a restringir por restringir.
4. Escolheu UM foco coerente com ranked_issues[0]?
Devolve APENAS: {"ok": true|false, "issues": ["..."], "report": {<o rascunho
corrigido, mesma forma; se ok, devolve-o tal como está>}}"""


def build_critic_prompt(diagnosis: Dict[str, Any], report: Dict[str, Any]) -> str:
    facts = {"ranked_issues": diagnosis.get("ranked_issues"),
             "nutrients": diagnosis.get("nutrients"),
             "adherence": diagnosis.get("adherence"),
             "wins": diagnosis.get("wins")}
    return (f"{_CRITIC_RULES}\n\nFACTOS:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"RASCUNHO:\n{json.dumps(report, ensure_ascii=False)}")


_NEXT_MEAL_RULES = """És um coach de nutrição a responder à pergunta diária "o que vou
comer?" em português de Portugal ("tu"). Recebes o que falta ao dia e, por nutriente em
falta, os alimentos que a pessoa JÁ come mais densos nesse nutriente, com o intervalo de
gramas que fecha a falha (já calculado — usa esses intervalos, não inventes gramas).

REGRAS:
- Monta 3 pratos realistas e apetecíveis. O 1.º é o recomendado.
- Usa sobretudo comida que a pessoa já come (a lista `candidates`). Podes introduzir no
  MÁXIMO 1 alimento novo saudável e comum por sugestão — nunca uma "bomba de nutrientes"
  que ninguém come. Marca o novo com "new": true.
- Respeita as gramas dadas (grams_low..grams_high). Não excedas as calorias que sobram.
- Cada prato diz, numa frase, o que resolve (o foco da semana e/ou a falha de hoje).
- Sê concreto e apetecível; isto tem de dar vontade de cozinhar."""

_PLATES_SCHEMA = """Devolve APENAS: {"plates": [
  {"rank": 1, "recommended": true, "title": "nome do prato",
   "items": [{"food": "...", "grams_low": N, "grams_high": N, "new": false}],
   "covers": [{"key": "omega3_g", "label": "Ómega-3", "note": "fecha a semana"}],
   "calories": N, "protein_g": N,
   "why": "uma frase — o que resolve e porque encaixa"},
  {"rank": 2, ...}, {"rank": 3, ...}
]}"""


def build_next_meal_prompt(context: Dict[str, Any],
                           profile: List[Dict[str, Any]]) -> str:
    top_foods = [{"food": f["food"], "category": f["category"],
                  "times_eaten": f["times_eaten"]} for f in profile[:25]]
    payload = {"context": context, "foods_the_user_eats": top_foods}
    return (f"{_NEXT_MEAL_RULES}\n\nDADOS (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n{_PLATES_SCHEMA}")


# -- model calls ---------------------------------------------------------------
def narrate_weekly(diagnosis, profile, continuity, *, dry_run: bool):
    prompt = build_weekly_prompt(diagnosis, profile, continuity)
    if dry_run:
        log.info("[dry-run] weekly prompt (%d chars):\n%s", len(prompt), prompt)
        return None, "dry-run"
    draft = claude_cli.call_claude_json(prompt, model=MODEL, effort=EFFORT,
                                        timeout_s=CALL_TIMEOUT_S, require_key="headline")
    # Critic pass — reconcile the prose against the facts, exactly as the audit
    # reconciles an estimate against the image.
    critic_prompt = build_critic_prompt(diagnosis, _strip_meta(draft))
    try:
        verdict = claude_cli.call_claude_json(
            critic_prompt, model=MODEL, effort=CRITIC_EFFORT,
            timeout_s=CALL_TIMEOUT_S, require_key="ok")
        report = verdict.get("report") or _strip_meta(draft)
        critic = "ok" if verdict.get("ok") else "corrected: " + "; ".join(
            verdict.get("issues", []))[:400]
    except claude_cli.ClaudeError as exc:
        log.warning("critic pass failed (non-fatal, keeping draft): %s", exc)
        report, critic = _strip_meta(draft), "critic-skipped"
    return _strip_meta(report), critic


def assemble_next_meal(context, profile, *, dry_run: bool):
    prompt = build_next_meal_prompt(context, profile)
    if dry_run:
        log.info("[dry-run] next-meal prompt (%d chars):\n%s", len(prompt), prompt)
        return None
    result = claude_cli.call_claude_json(prompt, model=MODEL, effort=EFFORT,
                                         timeout_s=CALL_TIMEOUT_S, require_key="plates")
    return result.get("plates")


def _strip_meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in obj.items() if not k.startswith("_")}


# -- modes ---------------------------------------------------------------------
def _sunday_ref(now: datetime) -> str:
    """The report is anchored on the most recent Sunday: the window is the 7 completed
    days before it. Running on Sunday reviews the week that just ended."""
    d = now.date()
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def run_weekly(sheets, *, dry_run: bool, date: Optional[str]) -> int:
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
    report, critic = narrate_weekly(diagnosis, profile, continuity, dry_run=dry_run)
    if dry_run:
        _write_profile(sheets, profile, dry_run=True)
        return 0

    focus_key = (report.get("focus", {}) or {}).get("key") or (
        diagnosis.get("ranked_issues") or [""])[0]
    focus_value = _nutrient_mean(diagnosis, focus_key)
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
        "model": MODEL,
        "critic_verdict": critic,
        "status": "generated",
    })
    _write_profile(sheets, profile, dry_run=False)
    log.info("weekly report written: focus=%s (%s), critic=%s", focus_key,
             (report.get("focus", {}) or {}).get("label", ""), critic)
    return 0


def run_next_meal(sheets, *, dry_run: bool) -> int:
    now = datetime.now(TZ)
    day = now.date().isoformat()

    focus_key = _current_focus(sheets) if not dry_run else None
    params = {"focus": focus_key} if focus_key else None
    context = backend_get("/insights/next-meal-context", params)
    profile = backend_get("/insights/food-profile").get("foods", [])
    if not context.get("candidates"):
        log.info("nothing short today — no next-meal suggestion needed.")
        return 0

    plates = assemble_next_meal(context, profile, dry_run=dry_run)
    if dry_run:
        return 0
    if not plates:
        log.warning("model returned no plates; leaving yesterday's cache untouched.")
        return 0

    ensure_tab(sheets, NEXT_MEAL_TAB, NEXT_MEAL_HEADERS)
    upsert_row(sheets, NEXT_MEAL_TAB, NEXT_MEAL_HEADERS, "date", {
        "date": day,
        "generated_at": now.isoformat(timespec="seconds"),
        "snapshot_json": json.dumps(context, ensure_ascii=False),
        "focus_key": focus_key or "",
        "plates_json": json.dumps(plates, ensure_ascii=False),
        "model": MODEL,
        "status": "generated",
    })
    log.info("next-meal written: %d plates for %s (focus=%s)", len(plates), day,
             focus_key or "—")
    return 0


def _current_focus(sheets) -> Optional[str]:
    rows = [r for r in read_rows(sheets, WEEKLY_TAB) if r.get("week_start")]
    if not rows:
        return None
    return str(max(rows, key=lambda r: str(r.get("week_start"))).get("focus_key") or "") \
        or None


def _write_profile(sheets, profile: List[Dict[str, Any]], *, dry_run: bool) -> None:
    """Persist the food vocabulary to an inspectable derived tab (rebuilt each run)."""
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
                        help="Fetch facts and print the prompt/plan without a model "
                             "call or any sheet write.")
    parser.add_argument("--date", default=None,
                        help="weekly: the week_start (a Sunday) to (re)generate.")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_DIR / "insights.log")])

    sheets = None
    if not args.dry_run:
        sheets = build("sheets", "v4", credentials=get_credentials(),
                       cache_discovery=False)
    if args.mode == "weekly":
        return run_weekly(sheets, dry_run=args.dry_run, date=args.date)
    return run_next_meal(sheets, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
