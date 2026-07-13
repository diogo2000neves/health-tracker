# Health Tracker

A zero-friction personal health pipeline. **Step 1** pulls your **weight and
body-fat** from the **Google Health API** (where your Goodvibes scale data lands
via Google Health) and stores it to disk. Later steps add Fitbit-Air biometrics
and AI-analysed food photos, aggregating one row per day into a Google Sheet.

## Architecture (the plan we're building toward)

```
Goodvibes scale ─► Google Health ─┐
Fitbit Air ───────► Google Health ─┼─► Google Health API ─► Python ─► data/bronze/*.json
Food photos ──────► (Step 3, AI)  ─┘        (this repo)         │
                                                                └─► daily aggregate ─► Google Sheet
```

- **Bronze** = raw dated JSON in `data/bronze/` (immutable, re-runnable).
- **Silver** = a daily Python job that normalises + builds one row per day.
- **Gold** = the Google Sheet (written only by that job, upsert on date).

Step 1 in this repo covers the bronze pull for weight.

## Layout

```
Health Tracker/
├── cloudbuild.yaml         # CI/CD: test gate → build → deploy all targets
├── Dockerfile              # image for the daily + weekly jobs
├── requirements.txt
├── credentials/            # OAuth client + token — git-ignored
├── data/bronze/            # raw API responses (local runs) — git-ignored
├── ingest/
│   ├── Dockerfile
│   ├── main.py             # Cloud Run service: POST /ingest (photo), POST /feel
│   └── requirements.txt
├── src/
│   ├── auth.py             # OAuth flow + token refresh (local)
│   ├── authenticate.py     # one-time login: python -m src.authenticate
│   ├── fetch_weight.py     # local debug pull: python -m src.fetch_weight
│   ├── google_health.py    # Google Health API v4 client (with retries)
│   ├── sheets.py           # schema + merge-upsert Sheet client
│   ├── run_daily.py        # daily job: physique + nutrition roll-up + dashboard
│   ├── weekly_insights.py  # weekly job: Gemini trend summary → `insights` tab
│   └── maintenance.py      # idempotent schema/dashboard sync (run after schema changes)
└── tests/                  # unit tests — the CI deploy gate
```

## Endpoints (Cloud Run service, `X-Auth-Token` gated)

- `POST /ingest` — one or more meal photos, a text description, or a mix.
  Accepts a raw image body, or a multipart form with any number of image file
  parts + an optional `note` text field (a `?note=` query param / JSON
  `{"note": …}` also works). Extra photos can be a nutrition label, packaging,
  or an ingredient the first shot missed — the AI reasons across all of them
  (a label is authoritative and scaled to the portion on the plate). The `note`
  is authoritative context ("only ate half" halves portions); a note with no
  image estimates the meal from text alone at capped confidence. De-dupes
  (combined image hash, or note hash when text-only) ignoring failed stubs.
  **Hybrid reliability:** a quick single-model pass gives the phone instant
  macros when Gemini is fast; if it's slow, the photos are archived and the meal
  is handed to a **Cloud Tasks** queue (`202 Queued`) that retries the analysis
  in the background until the row lands — so a transient Gemini outage can't lose
  a meal. Replies with the meal + running totals, or a queued ack.
- `POST /process` — internal Cloud Tasks worker: the thorough analysis + row
  insert. Returns 5xx to trigger a retry; writes an "analysis failed" stub only
  on the final attempt. Same `X-Auth-Token` gate; not called by the phone.
- `POST /feel` — `{"score": 1-10[, "date": "YYYY-MM-DD"]}` → writes
  `subjective_feel` on that day's `daily_summary` row (`{"score": null}` clears).

## Tests

```bash
python -m pytest tests -q
```
The same suite gates every deploy in Cloud Build — a red test means nothing
ships and production keeps the previous version.

## One-time Google Cloud setup

In the [Google Cloud Console](https://console.cloud.google.com/) for your project:

1. **Enable the API** — APIs & Services ▸ *Enable APIs and Services* ▸ enable
   **Google Health API**. (Calls return 403 until this is on.)
2. **OAuth consent screen** — add the scope
   `.../auth/googlehealth.health_metrics_and_measurements.readonly` and add your
   own Google account as a **Test user**.
   - ⚠️ While the app is in **Testing**, refresh tokens expire after **7 days**
     (you'd re-login weekly). To run unattended daily, set the publishing status
     to **In production** — for personal use you can click past the "unverified
     app" screen; the refresh token then persists.
3. **OAuth client** — must be type **Desktop app**. (If you made a *Web* client,
   either recreate it as Desktop, or add `http://localhost` to its authorized
   redirect URIs.)
4. `credentials/oauth_client.json` already holds your client id/secret.

> 🔐 **Rotate the client secret.** It was shared in chat, so treat it as exposed:
> APIs & Services ▸ Credentials ▸ your client ▸ *Reset secret*, then paste the new
> value into `credentials/oauth_client.json`. That file is git-ignored.

## Install

```bash
cd "Health Tracker"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# First time — opens a browser to log in and stores a refreshable token
python -m src.authenticate

# Step 1 — fetch weight + body-fat, save raw JSON, print the latest reading
python -m src.fetch_weight
```

Output lands in `data/bronze/weight_<timestamp>.json`.

## Continuous deployment

Pushing to `main` auto-builds and redeploys both Cloud Run targets via Cloud
Build (`cloudbuild.yaml`, trigger `health-tracker-deploy` in `europe-west1`):

- `health-tracker-daily` (Job) — built from `./Dockerfile`
- `health-tracker-ingest` (Service) — built from `./ingest/Dockerfile`

Images are tagged with the commit SHA; deploys swap only the image, so each
target keeps its env vars and Secret Manager bindings.
