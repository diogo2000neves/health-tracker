# Health Tracker — System Context

> Paste this into a new chat as background before asking for new features.
> No secrets are included here; all live in Google Secret Manager.

## 1. Goal & philosophy

A **zero-friction personal health dashboard**. The golden rule: I only perform the
inevitable physical actions of my day (step on the scale, wear my tracker, snap a
photo of what I eat). *Everything else* — collection, extraction, organisation —
is automated background work. If I have to open apps, type data, or export
reports, the system has failed.

The end goal is to **correlate nutrition against physique on a per-day basis**:
what I ate vs what my body did.

Constraints: 100% cloud (no laptop needed), near-zero ongoing cost, simple enough
for an AI agent to maintain, and **sovereign storage** — final data lives in a
Google Sheet + Google Drive that I own and can download anytime.

## 2. The three data sources

| # | Source | Hardware / input | Status |
|---|---|---|---|
| 1 | **Body composition** | Tefal **Goodvibes** smart scale | ✅ Built (weight + body-fat only) |
| 2 | **Biometrics / activity** | **Fitbit Air** (steps, sleep, HR) | ❌ **NOT built yet** |
| 3 | **Nutrition** | iPhone camera → meal photos | ✅ Built |

### Source 1 — Scale (weight, body fat)
Scale → Goodvibes app → Fitbit cloud → **Google Health**. A daily Cloud Run **Job**
pulls it via the **Google Health API** and writes one row per day.

**Hard limitation (already researched, don't redo):** the scale measures ~14
metrics (visceral fat, body water, muscle/bone mass, BMR, metabolic age…), but
**only weight, body-fat and height survive** the trip to Google Health — Fitbit
strips the rest. The Google Health API exposes *only* the data types `weight`,
`body-fat`, `height`. The rich metrics are computed on-device from bioimpedance
and exist only inside the Goodvibes app (no public API). The only way to capture
them would be reading the scale's **Bluetooth** signal directly with an always-on
device (e.g. `ble-scale-sync` on a Raspberry Pi). **Decision: not doing it.** We
deliberately keep just **weight + body_fat_pct**.

### Source 2 — Fitbit Air (NOT built)
This is the next obvious feature. It should reuse the *same* Google Health API and
the *same* OAuth token — just different `dataTypes` (steps, sleep, heart rate) and
extra scopes (`activity_and_fitness.readonly`, `sleep.readonly`). Unlike the
scale's body composition, this biometric data **is** fully available via the API.

### Source 3 — Meal photos
iPhone **Shortcut** (a button in Control Center) → takes a photo → HTTP **POST**s
it straight to a Cloud Run **service**. That service archives the photo to Google
Drive, sends it to **Gemini** for nutrition estimation, and appends a row to the
Sheet. Photos never touch iCloud or Google Photos (neither is readable by a
server — the Google Photos API can no longer read a user's library, and iCloud has
no server API at all).

## 3. Architecture

```
                    ┌──────────── Google Sheet "Health Tracker" ────────────┐
                    │   tab `daily`  : one row per day (weight, body fat)   │
                    │   tab `meals`  : one row per photo (foods, macros)    │
                    └──────────────────────────────────────────────────────┘
                              ▲                                ▲
   Cloud Scheduler            │                                │
   (07:00 Europe/Lisbon) ─► Cloud Run JOB              Cloud Run SERVICE ◄── iPhone Shortcut
                            health-tracker-daily       health-tracker-ingest    (POST photo)
                                    │                          │  │
                          Google Health API           Gemini API │ Google Drive
                          (weight, body-fat)         (nutrition) │ (photo archive)
```

- **Job vs Service:** the Job is a *pull* on a timer (scale data). The Service is a
  *push* endpoint that waits for the phone (photos). Both scale to zero.
- Everything lives in GCP project **`health-tracker-501322`**, region
  **`europe-west1`**.

## 4. Auth model (three identities, least-privilege)

| What | Identity | Why |
|---|---|---|
| Read Google Health API | **User OAuth token** | health data requires *user* consent; a service account can't read it |
| Write the Sheet | **Service account** | the Sheet is shared with it as Editor |
| Upload photos to Drive | **User OAuth token** (`drive.file`) | ⚠️ a service account has **zero Drive storage quota** — uploads must run as the user to use their 5 TB |
| Call Gemini | **API key** | free tier |

Service account: `health-tracker-job@health-tracker-501322.iam.gserviceaccount.com`
(has no project-level roles; only resource-level secret access + run.invoker).

## 5. Key resources

| Thing | Value |
|---|---|
| GCP project | `health-tracker-501322` (billing enabled) |
| Cloud Run Job | `health-tracker-daily` (europe-west1) |
| Cloud Run Service | `health-tracker-ingest` (europe-west1, public URL, gated by `X-Auth-Token` header) |
| Scheduler | `health-tracker-daily-trigger` — `0 7 * * *` Europe/Lisbon |
| Sheet ID | `1JQWYkSgzU3F4mqR7BRE8wfoBif0xLU7uBM0iwHwxNAk` |
| Drive photo folder | `1i0wYuIzcD7ifs_wVQVdsUpI26vGmJdfP` ("Health Tracker Meals", owned by user) |
| Secrets (Secret Manager) | `health-oauth-token`, `ingest-token`, `gemini-api-key` |
| Code (master copy) | `/Users/dneves/Health Tracker/` — `src/` (job), `ingest/` (service) |

### Sheet schema
- **`daily`**: `date | weight_kg | body_fat_pct | source | updated_at`
  - Only data from **2026-07-04 onward** (cutoff env `HEALTH_START_DATE`; history was deliberately purged).
  - Idempotent upsert keyed on `date`. Multiple weigh-ins/day → the **first of the day** wins.
- **`meals`**: `datetime | date | foods | calories | protein_g | carbs_g | fat_g | confidence | photo_url | portion_g | notes`

## 6. Gemini setup (important cost nuances)

- Uses the **Gemini Developer API** (AI Studio key), **not** Vertex AI.
- ⚠️ **Google AI Pro subscription does NOT grant API access.** It only provides the
  Gemini app + 5 TB storage.
- ⚠️ An AI Studio key on a project **with billing enabled silently uses the PAID
  tier**. So the key lives in the **billing-free** project
  `gen-lang-client-0757945342`. This is what makes it **€0**.
- Free tier ≈ 1,500 requests/day, **Flash models only**. Google may use free-tier
  data to improve their products (accepted).
- **Model fallback chain** (`GEMINI_MODELS` env):
  `gemini-3.5-flash → gemini-3-flash-preview → gemini-3.1-flash-lite`.
  The bigger Flash models are frequently `503 overloaded` on the free tier, so in
  practice **`gemini-3.1-flash-lite`** usually serves. The chain means a meal never
  fails to log.
- The prompt forces: precise food identification (tangerine ≠ orange), portion
  estimation in grams using a visual scale reference, nutrition computed for *that*
  portion, and a self-consistency check (kcal ≈ 4P + 4C + 9F).

## 7. Cost

Effectively **~€0.10/month** (container storage). Cloud Run / Scheduler / Secret
Manager are within free tiers; Gemini is free tier; photos use the user's own 5 TB
Google One storage (from AI Pro).

## 8. Gotchas learned (don't rediscover these)

1. Legacy **Fitbit Web API dies Sept 2026**; Google Fit REST dies end-2026. Use the
   **Google Health API** (`https://health.googleapis.com/v4`).
2. Google Health response shape is **nested**: `dataPoints[].weight.weightGrams`,
   `.weight.sampleTime.physicalTime` (UTC); body-fat is `.bodyFat.percentage`.
3. OAuth app must be **In production** (not "Testing"), or refresh tokens expire
   after 7 days.
4. Cloud Build fails until the **default compute SA** gets `roles/cloudbuild.builds.builder`.
5. `gcloud --set-env-vars` **breaks on commas inside a value** — use the delimiter
   syntax: `--set-env-vars "^@^VAR=a,b@VAR2=c"`.
6. Service accounts have **no Drive storage quota** → Drive uploads must use the
   user's OAuth token.
7. **Google Photos API cannot read your library** (since Mar 2025) and **iCloud has
   no server API** — that's why photos are POSTed directly to our endpoint.

## 9. Open TODOs

1. **The daily nutrition ↔ weight join** — roll each day's `meals` into total
   calories/macros and place them beside `weight_kg` / `body_fat_pct` on the
   `daily` tab. *This is the original end goal and is not done yet.*
2. **Fitbit Air biometrics** (steps, sleep, resting HR) — same API + token, new
   dataTypes and scopes.
3. Optional: a dashboard/chart (weight trend vs calorie intake).
4. Optional: put the code in a private GitHub repo (Cloud Run stores only built
   containers, not readable source).
5. Housekeeping: the `meals` tab still contains a few `not food` test rows.
6. Security: the OAuth **client secret** was once pasted in chat and should be
   rotated (existing tokens keep working).
