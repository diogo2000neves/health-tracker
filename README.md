# Health Tracker

A zero-friction personal health pipeline. Everything enters through **one button
on the phone**: photograph a meal, or screenshot your smart scale. Gemini works out
which of the two it's looking at, extracts the numbers, and files them into a
Google Sheet you own — one row per day, nutrition against physique.

```
                      iPhone Shortcut (one button)
                                 │
                photo of a meal ─┴─ screenshot of the scale app
                                 │
                                 ▼
                    health-tracker-ingest (Cloud Run)
                      "is this food, or a scale?"
                          │                │
              per-ingredient          all 10 body
              nutrition (AI)        metrics (AI OCR)
                          │                │
                          ▼                ▼
                    Google Sheet ── daily_summary (one row/day)
                          ▲
                   nightly roll-up
```

There is no scheduled *sync* of anything. Data lands the moment you send it; the
one Cloud Run **Job** only derives from what's already in the Sheet.

## Layout

```
Health Tracker/
├── cloudbuild.yaml         # CI/CD: test gate → build → deploy all targets
├── Dockerfile              # image for the daily + weekly jobs
├── requirements.txt
├── credentials/            # OAuth client + token — git-ignored
├── ingest/
│   ├── Dockerfile
│   ├── main.py             # Cloud Run service: POST /ingest, /process, /feel
│   └── requirements.txt
├── src/
│   ├── auth.py             # OAuth: two profiles (health / drive), never merged
│   ├── authenticate.py     # one-time login: python -m src.authenticate health|drive
│   ├── google_health.py    # Google Health API v4 client (list + dailyRollUp)
│   ├── biometrics.py       # Fitbit payloads → daily columns (pure, unit-tested)
│   ├── sheets.py           # schema + merge-upsert Sheet client
│   ├── run_daily.py        # daily job: biometrics + nutrition roll-up
│   └── maintenance.py      # idempotent schema/dashboard sync (run after schema changes)
└── tests/                  # unit tests — the CI deploy gate
```

## Endpoints (Cloud Run service, `X-Auth-Token` gated)

- `POST /ingest` — **a scale screenshot.** Recognised automatically (you don't say
  which kind of thing you're sending). All ten metrics the scale computes — weight,
  BMI, body fat, subcutaneous fat, visceral fat, body water, muscle mass, bone
  mass, BMR, metabolic age — are read off the screen and merged into
  `daily_summary`, plus a derived lean mass. The row is keyed on **the reading's own
  date, printed on the screen**, so weighing at 07:00 and sending at noon still
  lands on the right day — and screenshotting the app's history backfills old days.
  Sending a new reading for a day replaces it. Values are bounds-checked before
  they're written, so an OCR slip is dropped rather than stored.

- `POST /ingest` — **one or more meal photos, a text description, or a mix.**
  Accepts a raw image body, a JSON `images` array of base64 strings, or a multipart
  form with any number of image file parts + an optional `note` text field (a
  `?note=` query param / JSON `{"note": …}` also works). Extra photos can be a
  nutrition label, packaging, or an ingredient the first shot missed — the AI
  reasons across all of them (a label is authoritative and scaled to the portion on
  the plate). The `note` is authoritative context ("only ate half" halves
  portions); a note with no image estimates the meal from text alone at capped
  confidence. De-dupes (combined image hash, or note hash when text-only) ignoring
  failed stubs.
  **Hybrid reliability:** a quick single-model pass gives the phone instant macros
  when Gemini is fast; if it's slow, the photos are archived and the meal is handed
  to a **Cloud Tasks** queue (`202 Queued`) that retries the analysis in the
  background until the row lands — so a transient Gemini outage can't lose a meal.

- `POST /process` — internal Cloud Tasks worker: the thorough analysis + row
  insert. Returns 5xx to trigger a retry; writes an "analysis failed" stub only
  on the final attempt. Same `X-Auth-Token` gate; not called by the phone.

- `POST /ingest` — **a bowel-movement note.** A plain text note through the same
  note Shortcut — "fiz cocó", "I just pooped", any phrasing/language — sets
  `daily_summary.bowel_movement` = TRUE for the day (a blank cell is "no"). The
  model classifies every text note first: a bathroom report flags the day, anything
  describing food is estimated as a meal. Nothing from the note is stored; the whole
  feature is one boolean.

- `GET /daily?from=&to=&blocks=&tier=` — **the iOS app's read endpoint.** Days as
  JSON nested by block (`sleep`, `recovery`, `activity`, `nutrition`, `body`,
  `self_report`), defaulting to the last 30 days. `?blocks=sleep,recovery` fetches
  only the screen you're drawing; `?tier=1` trims to headline metrics. A blank cell
  comes back as `null`, never `0` — "not measured" is not "zero".

- `GET /schema` — the data dictionary as JSON: every column's unit, type, source,
  direction and **when it was measured**. Lets a client (or an agent) discover the
  data without shipping a copy of this repo.

The app talks to *this service*, never to Google. That's deliberate: reading Sheets
directly would need Google credentials inside the app bundle, and anything shipped
in an iOS binary is extractable. The service account stays server-side; the app
holds only a rotatable token. It also means storage can change later without
touching a line of Swift.

```bash
python -m src.schema_export swift > HealthTypes.swift   # Codable structs, generated
```

## Jobs

- **`health-tracker-daily`** (07:00 Europe/Lisbon) — pulls **Fitbit Air
  biometrics** from the Google Health API (~40 columns/day: sleep stages and
  efficiency, resting HR, HRV, SpO2, respiration, skin temperature, steps,
  distance, calories out, active/zone minutes, heart-rate range) and rolls the
  `meals` tab up into `daily_summary`'s nutrition columns. 07:00 is late enough
  that last night is scored and synced, and past the 05:00 nutrition cutoff so
  yesterday can be totalled.

### What the tracker gives you (and what it can't)

The Air syncs by itself — nothing to open, nothing to tap. **There is no sleep
score**: Fitbit's 0-100 number is proprietary and appears nowhere in the Google
Health API. What lands instead is the data it's computed *from* —
`sleep_efficiency_pct` (asleep ÷ in-bed), deep/REM/light minutes, latency,
awakenings — plus resting HR, HRV and skin-temperature deviation. Naps are tracked
separately (`nap_mins`) so an afternoon nap can't corrupt the night.

`total_cals_out` is the one to notice: paired with `total_cals_in`, it gives real
measured **energy balance**.

## Tests

```bash
python -m pytest tests -q
```
The same suite gates every deploy in Cloud Build — a red test means nothing
ships and production keeps the previous version.

## Schema changes

**`schema/registry.py` is the single source of truth.** Add a `Column` there —
with its unit, source, direction, causal window and description — then run

```bash
python -m src.maintenance
```

which realigns the sheet *by header name* (so adding, removing and reordering are
all safe), rewrites the `schema` tab, and reapplies the presentation. It refuses to
drop a column that still holds data. The daily job refuses to run against a stale
sheet rather than writing through it.

Everything downstream regenerates from that one definition: the header row, the
data dictionary, the OCR plausibility bands, the causal alignment, the app's JSON
and Swift types.

## The tabs

| tab | what it is |
|---|---|
| `daily_summary` | **the source of truth.** One row per local day, 79 columns, grouped into collapsible blocks. Every column is 1:1 with `date`. |
| `meals` | one row per meal; per-ingredient breakdown in `items` JSON |
| `templates` | meals weighed on a real scale — measured, not estimated |
| `analysis` | **derived, rebuilt every run.** Causally aligned: each day's inputs beside the *next* day's outcomes. Correlate here — see below. |
| `baselines` | **derived.** 28-day mean/SD/z per metric: what's normal *for you* |
| `schema` | **derived.** The data dictionary — what every column means |

### Why `analysis` exists

A `daily_summary` row is an *observation of a date*, not a causal unit. The sleep
on row N happened the night **before** N; the weight was measured that morning,
**before** you ate that day. So correlating `total_cals_in` against
`sleep_efficiency_pct` on the same row asks whether *tomorrow's* dinner affected
*last night's* sleep — backwards in time.

`analysis` fixes that: inputs from day N, outcomes (suffixed `_next`) from day N+1.
It's rebuilt from scratch every run, so it can't drift. **Correlate on `analysis`,
read facts from `daily_summary`.**

## Install

```bash
cd "Health Tracker"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Auth (one-off)

Two user tokens, which **must never be merged** — the Google Health API rejects any
token that also carries a Drive scope (`403 DISALLOWED_OAUTH_SCOPES`). Everything
else runs as a service account.

```bash
# Fitbit biometrics (daily job) — sleep + health metrics + activity, read-only
python -m src.authenticate health
gcloud secrets versions add health-oauth-token --data-file=credentials/token_health.json

# Meal-photo upload (ingest) — drive.file only; a service account has no Drive quota
python -m src.authenticate drive
gcloud secrets versions add drive-oauth-token --data-file=credentials/token_drive.json
```

Your OAuth app must be **In production** (not "Testing"), or the refresh token
expires after 7 days. For personal use you can click past the "unverified app"
screen.

> 🔐 **Rotate the client secret.** It was shared in chat, so treat it as exposed:
> APIs & Services ▸ Credentials ▸ your client ▸ *Reset secret*, then paste the new
> value into `credentials/oauth_client.json`. That file is git-ignored.

## Continuous deployment

Pushing to `main` auto-builds and redeploys all three Cloud Run targets via Cloud
Build (`cloudbuild.yaml`, trigger `health-tracker-deploy` in `europe-west1`):

- `health-tracker-daily` (Job) — built from `./Dockerfile`
- `health-tracker-ingest` (Service) — built from `./ingest/Dockerfile`

Images are tagged with the commit SHA; deploys swap only the image, so each
target's env vars and secret bindings are preserved.

See `CONTEXT.md` for the full system design, auth model, and the gotchas worth
not rediscovering.
