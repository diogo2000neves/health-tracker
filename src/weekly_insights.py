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

# Only columns with signal today; blank cells are expected and fine.
CSV_COLUMNS = [
    "date", "total_cals_in", "total_protein_g", "total_carbs_g", "total_fat_g",
    "weight_kg", "body_fat_pct", "lean_mass_kg", "subjective_feel",
    "sleep_score", "steps",
]

PROMPT_TEMPLATE = """You are a precise, no-nonsense personal health-data analyst.
Below is a CSV of my daily health tracking for the last {days} rows (some cells
are blank — metrics not yet collected; treat blanks as missing, not zero).

{csv}

Write your analysis as 4-6 short markdown bullets, in this spirit:
- Weight / lean-mass trend vs calorie & protein intake — cite actual numbers.
- Any notable pattern, correlation or anomaly worth my attention.
- Data gaps that most limit the analysis (be specific: which metric, how often).
- End with ONE concrete, specific action for next week.
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
