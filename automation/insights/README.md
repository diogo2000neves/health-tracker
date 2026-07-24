# Weekly insights & next-meal coach (Gemini-powered)

The deterministic analysis already happened in the backend (`ingest/insights.py`,
served at `/insights/*`). This directory contains the narration layer that turns
those finished facts into human coaching prose in Portuguese.

## Two generation paths

Since the migration from Claude CLI to Gemini API, generation happens in **two ways**:

### 1. On-demand via backend (Cloud Run)

The primary path. The backend calls Gemini API directly — no Mac required.

| mode | trigger | reads | generates |
| --- | --- | --- | --- |
| `weekly` | `POST /insights/generate-weekly` (Cloud Scheduler, manual) | diagnosis + food-profile | weekly report (cached in `/tmp`) |
| `next-meal` | `POST /insights/generate-next-meal` (iOS app, manual) | enhanced context + timing profile | 3 plates for the dynamically-determined next slot (cached in `/tmp`) |

The **weekly** run picks one focus, celebrates wins, proposes a swap, resolves the
continuity delta against last week's stored focus, and runs a **critic pass** that
rejects any claim the facts don't support.

The **next-meal** determines the next meal slot dynamically (via AI analyzing
current time, today's meals, and the user's historical eating patterns) and
generates 3 appropriate plates. Portion *ranges* come from the backend
(`portion_range`), never the model.

**Required:** `GEMINI_API_KEY` env var on the backend. Model configurable via
`GEMINI_NARRATOR_MODEL` (default `gemini-3.6-flash`).

### 2. Local script (automation fallback)

For debugging or if the backend lacks Gemini API access. Uses the same narrator
module (`backend/ingest/narrator.py`).

```bash
export GEMINI_API_KEY="..."           # or set in .env
export HEALTH_BACKEND_URL="https://..."
export INGEST_TOKEN="..."

# Dry run (prints prompt, no model call, no sheet write):
backend/venv/bin/python automation/insights/generate.py weekly    --dry-run
backend/venv/bin/python automation/insights/generate.py next-meal --dry-run

# Live:
backend/venv/bin/python automation/insights/generate.py weekly
backend/venv/bin/python automation/insights/generate.py next-meal
```

## Output shapes (the app's contract)

`weekly_reports.report_json`:
```jsonc
{ "headline", "wins": [{"title","detail"}],
  "focus": {"key","label","why","attribution","severity"},
  "swap": {"from","to","why"}, "continuity": "…|null", "encouragement" }
```

`next_meal.plates_json` (v2 — dynamic slot):
```jsonc
{ "next_slot": "almoço",
  "plates": [{ "rank", "recommended", "title",
     "items": [{"food","grams_low","grams_high","new"}],
     "covers": [{"key","label","note"}], "calories", "protein_g", "why" }] }
```

## Safety

Diet advice to someone in a deficit chasing recomposition. The prompts forbid medical
claims and any "eat less for its own sake" framing; the critic pass enforces that the
alarm level matches the nutrient policy (a cholesterol spike is never dramatised). If
the API is unavailable the app shows the last cached report — generation degrades
gracefully.
