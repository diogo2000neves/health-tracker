# Weekly insights & next-meal coach (local generation)

The strong-model half of Phase 2. The backend already did the deterministic analysis
(`ingest/insights.py`, served at `/insights/*`); this job fetches those facts and
narrates them with the subscription `claude` CLI — the strongest model available, which
only exists on this Mac. It writes to Google Sheets; the app reads the cached rows back
through the backend. **No model ever runs on a request path.**

## What it does

| mode | trigger | reads | writes |
| --- | --- | --- | --- |
| `weekly` | Sunday 09:00 | `/insights/diagnose`, `/insights/food-profile` | `weekly_reports` (upsert on `week_start`, immutable), `food_profile` (replaced) |
| `next-meal` | daily 17:00 | `/insights/next-meal-context`, `/insights/food-profile` | `next_meal` (upsert on `date`) |

The **weekly** run picks one focus, celebrates wins, proposes a swap, resolves the
continuity delta against last week's stored focus, and runs a **critic pass** that
rejects any claim the facts don't support (mirroring the audit's reconcile-against-truth
discipline). The **next-meal** run assembles three ranked plates from foods the user
already eats; the portion *ranges* come from the backend (`portion_range`), never the
model.

## Setup

Reuses the audit's Google token (`backend/credentials/token_nutrition_audit.json`).
Two extra env vars point it at the backend that holds the facts:

```bash
export HEALTH_BACKEND_URL="https://<cloud-run-service>"   # the app's base URL
export INGEST_TOKEN="<the app's X-Auth-Token>"            # same token the phone sends
```

Optional: `INSIGHTS_MODEL` (default `claude-sonnet-5`), `INSIGHTS_EFFORT`
(default `xhigh`).

## Run it

```bash
# See the facts + the exact prompt, with no model call and no sheet write:
backend/venv/bin/python automation/insights/generate.py weekly    --dry-run
backend/venv/bin/python automation/insights/generate.py next-meal --dry-run

# Live:
backend/venv/bin/python automation/insights/generate.py weekly
backend/venv/bin/python automation/insights/generate.py next-meal
```

`--dry-run` still fetches the facts (so it needs the two env vars) but neither calls the
model nor touches the sheet — it prints the assembled prompt so the advice can be
tuned before spending a call.

## Schedule (launchd)

Install the two plists (edit the `REPLACE_WITH_*` env values first):

```bash
cp automation/insights/com.dneves.insights-weekly.plist   ~/Library/LaunchAgents/
cp automation/insights/com.dneves.insights-nextmeal.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dneves.insights-weekly.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dneves.insights-nextmeal.plist
```

## Output shapes (the app's contract)

`weekly_reports.report_json`:

```jsonc
{ "headline", "wins": [{"title","detail"}],
  "focus": {"key","label","why","attribution","severity"},
  "swap": {"from","to","why"}, "continuity": "…|null", "encouragement" }
```

`next_meal.plates_json`:

```jsonc
[{ "rank", "recommended", "title",
   "items": [{"food","grams_low","grams_high","new"}],
   "covers": [{"key","label","note"}], "calories", "protein_g", "why" }]
```

## Safety

Diet advice to someone in a deficit chasing recomposition. The prompts forbid medical
claims and any "eat less for its own sake" framing; the critic enforces it and that the
alarm level matches the nutrient policy (a cholesterol spike is never dramatised). If
the Mac is offline the app shows the last good report — generation just waits.
