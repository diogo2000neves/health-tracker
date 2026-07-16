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

from src.sheets import DAILY_TAB, INSIGHTS_TAB, SheetClient

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
INSIGHTS_HEADERS = ["week_ending", "insights", "model", "updated_at"]

DEFAULT_MODELS = "gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite"
WINDOW_DAYS = 35

# Only columns with signal today; blank cells are expected and fine (body columns
# are filled only on days the user weighed in, readiness columns not at all yet).
# The full body-composition block is here because it's the whole point of the
# analysis: muscle vs fat vs water is what says whether the intake is working.
CSV_COLUMNS = [
    "date",
    # intake
    "total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g",
    "total_fiber_g", "total_sugar_g", "total_saturated_fat_g", "total_sodium_mg",
    # body composition
    "weight_kg", "bmi", "body_fat_pct", "subcutaneous_fat_pct", "visceral_fat",
    "body_water_pct", "muscle_mass_kg", "bone_mass_kg", "lean_mass_kg",
    "bmr_kcal", "metabolic_age",
    # sleep
    "sleep_mins", "sleep_efficiency_pct", "sleep_deep_mins", "sleep_rem_mins",
    "sleep_light_mins", "sleep_latency_mins", "sleep_awakenings", "nap_mins",
    "sleep_start", "sleep_end",
    # overnight recovery
    "resting_hr_bpm", "hrv_ms", "spo2_pct", "respiratory_rate_brpm",
    "skin_temp_dev",
    # activity / expenditure
    "steps", "distance_km", "total_cals_out", "active_cals",
    "total_active_mins", "sedentary_mins", "hr_avg_bpm", "hr_max_bpm",
    # self-reported
    "subjective_feel", "bowel_movement",
]

PROMPT_TEMPLATE = """You are a precise, no-nonsense personal health-data analyst.
Below is a CSV of my daily health tracking for the last {days} rows (some cells
are blank — metrics not yet collected; treat blanks as missing, not zero).

{csv}

Write your analysis as 5-7 short markdown bullets, in this spirit:
- Body-composition trend vs intake — cite actual numbers. Weight alone is noise;
  read muscle mass, lean mass and body fat % together against calories and
  protein, and say whether the change is muscle, fat or water.
- **Energy balance**: `total_cals_in` vs `total_cals_out` (measured expenditure,
  not an estimate). State the actual surplus/deficit and whether the body-
  composition trend is consistent with it.
- **Sleep and recovery**: `sleep_efficiency_pct` (asleep/in-bed) plus deep and REM
  minutes, read against `resting_hr_bpm`, `hrv_ms` and `skin_temp_dev` — a rising
  resting HR, falling HRV or a positive temperature deviation is the classic
  under-recovery signature. Note any link to late/large meals or alcohol.
- Any other notable pattern, correlation or anomaly. `bowel_movement` is TRUE on
  days I had one (blank = none logged); note regularity and any link to fibre or
  intake if the data supports it — don't over-read sparse data.
- Data gaps that most limit the analysis (be specific: which metric, how often).
- End with ONE concrete, specific action for next week.
There is deliberately no sleep score — it does not exist in the source API. Do not
ask for one; reason from efficiency, stages and the recovery metrics instead.
No preamble, no disclaimers, no generic health advice — only what the data shows."""


def _to_csv(rows: List[Dict[str, Any]]) -> str:
    lines = [",".join(CSV_COLUMNS)]
    for row in rows:
        lines.append(",".join(str(row.get(c, "") if row.get(c) is not None else "")
                              for c in CSV_COLUMNS))
    return "\n".join(lines)


def generate(csv: str, days: int, api_key: str, models: List[str]) -> Dict[str, str]:
    client = genai.Client(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(days=days, csv=csv)
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

    rows = sorted(sheet.read_rows(DAILY_TAB), key=lambda r: str(r.get("date", "")))
    rows = rows[-WINDOW_DAYS:]
    if not rows:
        print("no daily_summary data yet — skipping")
        return

    result = generate(_to_csv(rows), len(rows), api_key, models)

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
