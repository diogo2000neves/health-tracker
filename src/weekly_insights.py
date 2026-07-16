"""Weekly AI trend summary: read the last ~5 weeks of daily_summary, ask Gemini
for a short, numeric analysis, and append it to the `insights` tab.

Runs as a Cloud Run Job (Sundays 20:00 Europe/Lisbon). Uses the same free-tier
AI Studio key and model-fallback chain as the ingest service.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import google.auth
from google import genai
from google.genai import types

from src.analysis import (
    BASELINE_HEADERS, analysis_headers, analysis_rows, baseline_rows,
)
from src.sheets import DAILY_TAB, INSIGHTS_TAB, SheetClient

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
INSIGHTS_HEADERS = ["week_ending", "insights", "model", "updated_at"]

DEFAULT_MODELS = "gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite"
WINDOW_DAYS = 35

# There is no hand-picked column list any more: the analysis view decides what the
# model sees, and it derives that from the registry's causal fields. A curated list
# here would be a third place for the schema to drift.

PROMPT_TEMPLATE = """You are a precise, no-nonsense personal health-data analyst.

Below are two CSVs of my personal health tracking. Blank means NOT MEASURED —
never treat a blank as zero.

=== 1. CAUSALLY ALIGNED DATA (last {days} days) ===
Each row pairs what I DID on `date` with what my body did AFTERWARDS: every column
ending `_next` is read from the FOLLOWING day's record. This is deliberate. My
sleep on a given date happened the night BEFORE that date, and my morning weight
was measured before I ate that day — so correlating intake against same-date sleep
or weight in a raw table asks whether tomorrow's dinner affected last night's
sleep. Use these `_next` pairings for every cause-and-effect claim you make.

{csv}

=== 2. WHAT IS NORMAL FOR ME (trailing 28 days) ===
Absolute values mean little across people: an HRV of 73 ms is excellent for one
person and a warning for another. Judge every reading against MY baseline below —
`latest_z` is standard deviations from my own mean, and `direction` says which way
is good for that metric.

{baselines}

Write your analysis as 5-7 short markdown bullets, in this spirit:
- Body-composition trend vs intake — cite actual numbers. Weight alone is noise;
  read muscle mass, lean mass and body fat % together against calories and
  protein, and say whether the change is muscle, fat or water.
- **Energy balance**: `total_cals_in` vs `total_cals_out` (measured expenditure,
  not an estimate). State the actual surplus/deficit and whether the body-
  composition trend is consistent with it.
- **Sleep and recovery**: `sleep_efficiency_pct_next` (asleep/in-bed) plus deep and
  REM minutes, read against `resting_hr_bpm_next`, `hrv_ms_next` and
  `skin_temp_dev_next` — a rising resting HR, falling HRV or a positive temperature
  deviation is the classic under-recovery signature. Say which of MY days' intake
  preceded the bad nights.
- Any other notable pattern, correlation or anomaly. `bowel_movement` is TRUE on
  days I had one (blank = none logged); note regularity and any link to fibre or
  intake if the data supports it — don't over-read sparse data.
- Data gaps that most limit the analysis (be specific: which metric, how often).
- End with ONE concrete, specific action for next week.
There is deliberately no sleep score — it does not exist in the source API. Do not
ask for one; reason from efficiency, stages and the recovery metrics instead.
No preamble, no disclaimers, no generic health advice — only what the data shows."""


def _to_csv(header: List[str], rows: List[List[Any]]) -> str:
    """CSV, not JSON: for a fixed-schema numeric table JSON costs ~5x the tokens
    (it repeats every key on every row) and buys nothing a header row doesn't."""
    lines = [",".join(str(h) for h in header)]
    for row in rows:
        lines.append(",".join("" if v is None else str(v) for v in row))
    return "\n".join(lines)


def generate(csv: str, baselines: str, days: int, api_key: str,
             models: List[str]) -> Dict[str, str]:
    client = genai.Client(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(days=days, csv=csv, baselines=baselines)
    last_err: Exception | None = None
    for model in models:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.4),
            )
            text = (resp.text or "").strip()
            if text:
                return {"text": text, "model": model}
        except Exception as err:
            last_err = err
    raise RuntimeError(f"all models failed ({models}); last error: {last_err}")


def main() -> None:
    spreadsheet_id = os.environ["HEALTH_SPREADSHEET_ID"]
    api_key = os.environ["GEMINI_API_KEY"]
    models = [m.strip() for m in
              os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
    tz = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))

    creds, project = google.auth.default(scopes=[SHEETS_SCOPE])
    sheet = SheetClient(creds, spreadsheet_id)

    daily = sorted(sheet.read_rows(DAILY_TAB), key=lambda r: str(r.get("date", "")))
    daily = daily[-WINDOW_DAYS:]
    if not daily:
        print("no daily_summary data yet — skipping")
        return

    # Analyse the causally aligned view, never the raw observations: on the raw
    # table a day's intake sits beside the night that preceded it, so any
    # correlation drawn from it runs backwards in time. Built here rather than read
    # from the tab so the weekly job doesn't depend on the daily job having run.
    aligned = analysis_rows(daily)
    baselines = baseline_rows(daily, today=datetime.now(tz).date())

    result = generate(
        _to_csv(analysis_headers(), aligned),
        _to_csv(BASELINE_HEADERS, baselines),
        len(aligned), api_key, models,
    )
    rows = aligned

    sheet.ensure_tab(INSIGHTS_TAB, INSIGHTS_HEADERS)
    sheet.append_row(INSIGHTS_TAB, [
        datetime.now(tz).date().isoformat(),
        result["text"],
        result["model"],
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    ])
    print(
        f"insights: analysed {len(rows)} days with {result['model']}, "
        f"appended to '{INSIGHTS_TAB}' (spreadsheet {spreadsheet_id}, project {project})."
    )


if __name__ == "__main__":
    main()
