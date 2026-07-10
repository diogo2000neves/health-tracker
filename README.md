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
├── requirements.txt
├── credentials/
│   ├── oauth_client.json   # your GCP OAuth (Desktop) client — git-ignored
│   └── token.json          # created on first login — git-ignored
├── data/bronze/            # raw API responses land here
└── src/
    ├── auth.py             # OAuth flow + token refresh
    ├── google_health.py    # tiny Google Health API v4 client
    ├── authenticate.py     # one-time login: python -m src.authenticate
    └── fetch_weight.py     # Step 1 entry point: python -m src.fetch_weight
```

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
