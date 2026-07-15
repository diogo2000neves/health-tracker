# Health Tracker — System Context

> Paste this into a new chat as background before asking for new features.
> No secrets are included here; all live in Google Secret Manager.

## 1. Goal & philosophy

A **zero-friction personal health dashboard**. The golden rule: I only perform the
inevitable physical actions of my day (step on the scale, wear my tracker, snap a
photo of what I eat). *Everything else* — collection, extraction, organisation —
is automated background work. If I have to type data or export reports, the system
has failed.

The rule is about *added* effort, not zero taps. The scale forces its app open to
sync at all — so a screenshot of that app, sent with the same button as a meal
photo, adds nothing to the ritual while yielding three times the data (see source
1). Prefer capturing what the user is *already forced to do* over building a
pipeline around it.

The end goal is to **correlate nutrition against physique on a per-day basis**:
what I ate vs what my body did.

Constraints: 100% cloud (no laptop needed), near-zero ongoing cost, simple enough
for an AI agent to maintain, and **sovereign storage** — final data lives in a
Google Sheet + Google Drive that I own and can download anytime.

## 2. The three data sources

| # | Source | Hardware / input | Status |
|---|---|---|---|
| 1 | **Body composition** | Tefal **Goodvibes** smart scale → app screenshot | ✅ Built (all 10 metrics) |
| 2 | **Biometrics / activity** | **Fitbit Air** (steps, sleep, HR) | ❌ **NOT built yet** |
| 3 | **Nutrition** | iPhone camera → meal photos | ✅ Built |

### Source 1 — Scale (10 metrics, via a screenshot)
Step on the scale, open the Goodvibes app (which is the only way it syncs at all),
**screenshot the result screen and send it through the same Shortcut button as a
meal photo**. The ingest service recognises it is a scale screenshot rather than
food, reads every value off it with Gemini, and writes them straight into
`daily_summary`.

We capture all ten metrics the scale computes: `weight_kg`, `bmi`,
`body_fat_pct`, `subcutaneous_fat_pct`, `visceral_fat`, `body_water_pct`,
`muscle_mass_kg`, `bone_mass_kg`, `bmr_kcal`, `metabolic_age` — plus a derived
`lean_mass_kg` and the reading's own `body_measured_at` timestamp.

**Why not the Google Health API (we used to, don't go back):** the scale measures
~14 metrics, but **only weight, body-fat and height survive** the trip through
Fitbit's cloud into Google Health — the rest are computed on-device from
bioimpedance and exist only inside the Goodvibes app, which has no public API.
Worse, the scale **only syncs while the app is open in the foreground anyway**. So
the API path cost a daily OAuth pull, a scheduled job and a token that couldn't be
shared with Drive — and still gave us 3 metrics out of 10, only if we'd remembered
to open the app. Since opening the app is unavoidable, screenshotting it is free.
The screenshot gives the full set, immediately, with no scheduled pull and no user
OAuth. **The Google Health API integration has been deleted.**

Two properties worth knowing:
* **The screenshot is self-dating.** The app prints the reading's date and time
  ("4 de julho de 2026 às 19:03"); the row is keyed on **that**, not on when the
  photo was sent. Weigh at 07:00, send at noon — it still lands on the right day.
  Re-sending an old screenshot rewrites its own historical row, which also means
  scrolling the app's history and screenshotting old readings **backfills** them.
* **The delta block is a trap.** These apps print a "since \<date\>" summary of
  *changes* above the real values, using the same labels (`+ 5.35 kg Peso`). The
  prompt explicitly rejects it, and `_normalize_body` enforces plausibility bands
  (weight 20–300 kg, BMI 8–70, …) so a misread is dropped rather than written.

### Source 2 — Fitbit Air (NOT built)
The next obvious feature, and now the *only* reason to bring back a user OAuth
token for health data. Steps, sleep stages, HRV, SpO2 and resting HR **are** fully
available via the **Google Health API** (unlike the scale's body composition),
using scopes `activity_and_fitness.readonly` + `sleep.readonly`. It would fill the
readiness columns through the same merge-upsert. Note the token would have to be
**health-only** — see gotcha 8.

### Source 3 — Meal photos
iPhone **Shortcut** (a button in Control Center) → takes a photo → HTTP **POST**s
it straight to a Cloud Run **service**. That service archives the photo to Google
Drive, sends it to **Gemini** for nutrition estimation, and appends a row to the
Sheet. Photos never touch iCloud or Google Photos (neither is readable by a
server — the Google Photos API can no longer read a user's library, and iCloud has
no server API at all).

**Multiple photos per meal** are supported — the multipart form can carry any
number of image parts (meal shot + a nutrition label + packaging + a missing
ingredient). Gemini reasons across **all** of them: a nutrition label is
authoritative for its product and gets scaled to the portion seen on the plate,
packaging pins the brand, and extra-ingredient shots add items — without
double-counting a food that appears in more than one photo. All photos are
archived to Drive and their links space-joined into `meals.photo_url`.

An **optional free-text `note`** can ride along (multipart form field, `?note=`
query, or JSON). With a photo it's **authoritative** context that overrides the
visual estimate ("only ate half" → halves portions; "air-fried, no oil" → drops
the hidden-fat guess). A note with **no photo** is a **text-only meal** — Gemini
estimates from the description alone at **capped confidence (≤0.50)**, nothing is
archived to Drive, and the raw note is stored in `meals.note` for provenance.

Whenever a **note says when the meal was eaten** — text-only *or* a photo logged
after the fact ("this yogurt with my lunch") — Gemini infers the **hour** into a
`meal_time` field; the server stamps the row with **today's date at that hour**
(never a future time). The `meals` tab is **sorted by `datetime` after every
append**, so back-dated rows slot into chronological order. Scope is **today
only** — other dates aren't parsed yet. A photo with **no timing note** keeps its
capture time (the model leaves `meal_time` empty).

### Source 4 — Bowel-movement note (a boolean per day)
A plain text note through the **same note Shortcut** — "fiz cocó", "I just pooped",
any phrasing, any language — sets `daily_summary.bowel_movement` = TRUE for the
day. No new button and nothing stored from the note text; the whole feature is one
boolean (the user goes at most once a day, so yes/no suffices; a blank cell is
"no").

Routing mirrors the image path's meal-vs-body fork: every text note is classified
first (`TEXT_ROUTER_PREFIX`). `kind:"bowel"` → flag the day and return; anything
describing food → the normal meal estimate. The classifier is deliberately narrow —
a note that mentions food wins as a meal even if it also mentions the bathroom, so
a real meal log is never swallowed. A text note can only ever be a meal or a bowel
log, never a scale reading (there's no screen to OCR), so `analyze_text` runs with
`allow_bowel=True, allow_body=False`. Setting the flag is idempotent (a re-send or a
Cloud Tasks retry just re-sets TRUE). Keyed on the **local day the note was sent**,
like `/feel` — not the waking-day grain nutrition uses.

## 3. Architecture

```
              ┌────────────────── Google Sheet "Health Tracker" ──────────────────┐
              │ `daily_summary` : one row/day (readiness + nutrition + physique)  │
              │ `meals`         : one row/photo (per-ingredient `items` JSON)     │
              │ `templates`     : measured, reusable meals                        │
              │ `dashboard`     : stat cells + embedded charts                    │
              │ `insights`      : weekly AI trend summaries                       │
              └────────────────────────────────────────────────────────────────────┘
                     ▲                    ▲                       ▲
  Cloud Scheduler    │   Cloud Scheduler  │                       │
  (07:00 Lisbon) ─► JOB   (Sun 20:00) ─► JOB              SERVICE ◄── iPhone Shortcut
       health-tracker-daily   health-tracker-weekly   health-tracker-ingest
               │                      │                    │  │   (POST /ingest — a meal
        (Sheet only —            Gemini API        Gemini API │    photo, a scale
         no user OAuth,          (insights)      (nutrition + │    screenshot, or a text
         no health API)                          body OCR +   │    note that's a meal or
               │                                 note router) │    a bowel log; the model
    rolls `meals` up into                                     │    routes it. POST /feel)
    the nutrition columns                            Google Drive (meal photos only)
```

- **Job vs Service:** the Service is a *push* endpoint that waits for the phone —
  it is where **all** user data now enters the system (meals *and* body
  composition). The Jobs are timers that only ever *derive* from what's already in
  the Sheet. All scale to zero.
- **Why the daily job still exists** (it is no longer a "sync"): nutrition uses a
  **waking-day** grain and a day is only totalled once it is *over* (see below), so
  something has to run after the day ends. That is its whole remaining purpose,
  plus refreshing the dashboard. It holds no OAuth token and calls no external API.
- **Meal ingestion is hybrid (reliability):** `/ingest` does a quick single-model
  pass for **instant macros** when Gemini is fast; if it's slow/unavailable, it
  archives the photos and enqueues a **Cloud Tasks** job (queue `meal-ingest`,
  europe-west1) that retries `/process` with backoff for ~15 min until the row
  lands (`202 Queued` to the phone meanwhile). This is what makes insertion
  ~99.9% — a transient Gemini 504 no longer loses or blocks a meal. On the final
  retry the worker writes an "analysis failed" stub so nothing is ever lost.
  Model chain is **flash-lite first** (fast/reliable), stronger flash models
  follow for the worker's thorough pass.
- **CI/CD:** pushing to `main` on GitHub runs `cloudbuild.yaml` — unit tests
  gate the build, then all three targets are rebuilt and redeployed (images
  tagged with the commit SHA; env vars/secrets preserved). Trigger:
  `health-tracker-deploy` (europe-west1).
- **Day grain:** every `date` is the **local civil day** (Europe/Lisbon / the
  device's own utcOffset) — never the UTC day. **Nutrition** uses a **waking-day**
  grain instead (`NUTRITION_DAY_CUTOFF_HOUR`, 05:00): a meal before the cutoff
  counts toward the previous day (a 00:17 pre-bed dessert → yesterday), and the
  still-in-progress day is **not** totalled until it ends (no partial-day sums).
- Everything lives in GCP project **`health-tracker-501322`**, region
  **`europe-west1`**. Log-based alert policies email on any job error, ingest
  error, or failed build.

## 4. Auth model (least-privilege)

| What | Identity | Why |
|---|---|---|
| Write the Sheet | **Service account** | the Sheet is shared with it as Editor |
| Upload photos to Drive | **User OAuth token** (`drive.file` only) | ⚠️ a service account has **zero Drive storage quota** — uploads must run as the user to use their 5 TB |
| Call Gemini | **API key** | free tier |

Service account: `health-tracker-job@health-tracker-501322.iam.gserviceaccount.com`
(has no project-level roles; only resource-level secret access + run.invoker).

Dropping Google Health left **exactly one user token** in the system, held by the
ingest service purely to write meal photos into the user's own Drive
(`drive-oauth-token`). The daily job now runs with no user identity at all.

## 5. Key resources

| Thing | Value |
|---|---|
| GCP project | `health-tracker-501322` (billing enabled) |
| Cloud Run Job | `health-tracker-daily` (europe-west1) |
| Cloud Run Service | `health-tracker-ingest` (europe-west1, public URL, gated by `X-Auth-Token` header) |
| Scheduler | `health-tracker-daily-trigger` — `0 7 * * *` Europe/Lisbon (nutrition roll-up only) |
| Cloud Tasks queue | `meal-ingest` (europe-west1) — background meal analysis, 8 attempts / ~15 min backoff; SA has `cloudtasks.enqueuer` |
| Sheet ID | `1JQWYkSgzU3F4mqR7BRE8wfoBif0xLU7uBM0iwHwxNAk` |
| Drive photo folder | `1i0wYuIzcD7ifs_wVQVdsUpI26vGmJdfP` ("Health Tracker Meals", owned by user) |
| Secrets (Secret Manager) | `drive-oauth-token`, `ingest-token`, `gemini-api-key` |
| Code (master copy) | `/Users/dneves/Health Tracker/` — `src/` (job), `ingest/` (service) |

### Sheet schema
- **`daily_summary`** (41 columns): `date` | readiness + self-report
  (`sleep_score, hrv_ms, spo2_pct, skin_temp_dev, subjective_feel,
  bowel_movement`) | macros (`total_cals_in, total_protein_g, total_carbs_g,
  total_fat_g`) | **15 Tier-1 micronutrient totals** (`total_fiber_g,
  total_sugar_g, total_saturated_fat_g, total_sodium_mg, total_potassium_mg,
  total_calcium_mg, total_iron_mg, total_magnesium_mg, total_zinc_mg,
  total_vitamin_c_mg, total_vitamin_d_ug, total_vitamin_b12_ug, total_vitamin_a_ug,
  total_folate_ug, total_omega3_g`) | activity (`total_active_mins, steps`) |
  **body** (`weight_kg, bmi, body_fat_pct, subcutaneous_fat_pct, visceral_fat,
  body_water_pct, muscle_mass_kg, bone_mass_kg, bmr_kcal, metabolic_age,
  lean_mass_kg, body_measured_at`) | `updated_at`. Column order lives in
  `src.sheets.DAILY_HEADERS`; the scale metrics are `src.sheets.BODY_METRICS`,
  **mirrored** in `ingest/main.py BODY_METRICS` (which also owns their plausibility
  bands) because ingest is a separate image and can't import `src`. A test asserts
  the two stay in step. `bowel_movement` is a TRUE/blank self-report (see source 4).
  - **Merge-upsert keyed on `date`** — each source fills only its own columns
    (scale screenshot → body, meals roll-up → nutrition, /feel → subjective_feel,
    a bowel note → bowel_movement; Fitbit biometrics will fill the readiness
    block). Never overwrites a column it doesn't own.
  - Nutrition columns are keyed on the **waking day** (05:00 cutoff) and only
    written once that day is **over**, so a late-night snack joins the right day
    and today's row shows no nutrition total until tomorrow morning's run.
  - Body columns are keyed on the reading's **own** date, read off the screenshot.
    Several weigh-ins in a day → the **last one sent wins** (it just overwrites).
    `lean_mass_kg = weight_kg × (1 − body_fat_pct/100)` is derived at write time.
  - The daily job re-rolls a trailing `HEALTH_RECONCILE_DAYS` (7) window; set 0
    + `HEALTH_START_DATE=2000-01-01` for a full backfill run.
- **`meals`**: `datetime | foods | items | calories | protein_g | carbs_g | fat_g |
  confidence | model | photo_url | portion_g | image_sha | note | template`
  - `note` = the user's optional free-text description (empty for most rows);
    stored for provenance, especially for text-only meals (empty `photo_url`).
  - `template` = which measured template supplied the numbers (blank = estimated
    from the photo). See `templates` below.
- **`templates`**: `name | description | items | portion_g | calories |
  protein_g | carbs_g | fat_g | created_at | updated_at` — meals the user weighed
  on a **real scale**, so their `items` (same per-ingredient JSON shape as
  `meals`) are **measured, not estimated**.
  - **Created from a note**: logging a weighed meal with a note that asks to save
    it as a template (any phrasing) persists its items under that name — no extra
    step in the Shortcut. Re-saving the same name updates it in place.
  - **Matched automatically**: every analysis gets a compact catalogue of the
    templates injected into the prompt. If the model recognises the dish it
    returns the template's name, and the server **swaps its estimate for the
    measured values** — so a repeat meal yields *identical* numbers every day
    (confidence `0.95`). `template_scale` handles "only ate half".
  - **Guardrails**: the model must be confident it's the same dish; a name it
    invents is rejected (the estimate is kept); the `meals.template` column
    records every application for audit; the note always wins (it can suppress a
    match or scale it), and a corrected note re-analyses and replaces the row.
  - `items` = JSON array, one object per ingredient with its portion, macros,
    `cooking_method` and a `nutrients` map (~36 possible nutrients, only the
    non-negligible ones stored). The flat columns are the row totals; the daily
    job sums the Tier-1 nutrients from `items` into `daily_summary`.
  - `confidence` uses a fixed 0.1–1.0 rubric (model-independent); `model` records
    which AI analysed the photo (audit); `image_sha` de-duplicates double-taps.
  - Rows with foods `not food` / `analysis failed` (or all-zero macros) are
    excluded from every roll-up.
- **Schema changes**: add the column to `DAILY_HEADERS` and run
  `python -m src.maintenance` (inserts the column in place so history stays
  aligned). Never reorder or rename existing columns.

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
  `gemini-3.1-flash-lite → gemini-3.5-flash → gemini-3-flash-preview`.
  flash-lite goes **first** deliberately: the bigger Flash models 503 on most
  free-tier calls (10–40 s of wasted fallback latency), and a *consistent*
  estimator produces cleaner day-to-day trend deltas than a mix of models with
  different biases. If every model fails, the photo is archived and an
  `analysis failed` stub row is logged — a meal is never silently lost.
- Output is enforced with a typed `response_schema` (structured JSON), not
  prompt-format begging, with a `reasoning` field generated FIRST so the model
  works through scale and hidden fats before committing to numbers — that
  ordering is the main accuracy lever. The prompt forces: opportunistic scale
  calibration (use whatever reference is actually in frame, apply typical sizes
  only when sure, lower confidence when none exists), a dedicated hidden-fats
  step (absorbed cooking oil, sauces, added sugar — the biggest calorie-error
  source), per-ingredient breakdown with cooking method, weight from size +
  density including occluded food, and a per-item kcal ≈ 4P + 4C + 9F check.
  Meal totals are summed in code from the items, never by the model (avoids
  arithmetic errors).

## 7. Cost

Effectively **~€0.10/month** (container storage). Cloud Run / Scheduler / Secret
Manager are within free tiers; Gemini is free tier; photos use the user's own 5 TB
Google One storage (from AI Pro).

## 8. Gotchas learned (don't rediscover these)

1. **The Goodvibes scale only syncs while its app is open in the foreground.** No
   amount of cloud plumbing avoids opening the app — which is exactly why
   screenshotting it costs nothing extra, and why the Google Health pull was never
   the "zero-touch" win it looked like.
2. **Body composition dies in transit to Google Health.** Fitbit's cloud keeps only
   weight/body-fat/height; the other seven metrics never leave the phone app. Don't
   re-attempt the API route for body composition — it cannot work. (Fitbit Air's
   *biometrics* are a different story and **are** available; see source 2.)
3. **Reading numbers off a screen is where a model lies most convincingly.** Always
   pair OCR with a plausibility band (`ingest/main.py BODY_METRICS`) and never let
   an unvalidated number reach the sheet. The specific trap here is the app's
   "since \<date\>" **delta block**, which uses identical labels to the real values.
4. OAuth app must be **In production** (not "Testing"), or refresh tokens expire
   after 7 days.
5. Cloud Build fails until the **default compute SA** gets `roles/cloudbuild.builds.builder`.
6. `gcloud --set-env-vars` **breaks on commas inside a value** — use the delimiter
   syntax: `--set-env-vars "^@^VAR=a,b@VAR2=c"`.
7. Service accounts have **no Drive storage quota** → Drive uploads must use the
   user's OAuth token. This is the only reason a user token still exists.
8. **The Google Health API rejects any token that also carries a Drive scope**
   (`403 DISALLOWED_OAUTH_SCOPES`) — one token can never serve both. Moot today
   (we hold a Drive-only token), but it will bite again the moment Fitbit Air
   biometrics need a health token: it must be a **separate secret**, not an extra
   scope on `drive-oauth-token`.
9. **Google Photos API cannot read your library** (since Mar 2025) and **iCloud has
   no server API** — that's why photos are POSTed directly to our endpoint.
10. The sheet's **European locale renders decimals with commas** — every Sheets
    read must use `valueRenderOption="UNFORMATTED_VALUE"`, or `float("7,8")`
    silently zeroes the numbers. Avoid locale-sensitive formulas; charts and
    stats are written via the API instead.
11. **Read ranges must be wide enough for the schema.** `daily_summary` is already
    40 columns; an `A1:Z` read silently truncates the header, so a column past the
    cut looks "missing" and its writes land nowhere. Reads go to `BZ`.
12. A day is the **local civil day** (Europe/Lisbon), never the UTC day. Never
    `[:10]` a UTC timestamp to get it.

## 9. Open TODOs

1. **Fitbit Air biometrics** (steps, sleep stages, HRV, SpO2, resting HR) — the
   Google Health API, new dataTypes, scopes `activity_and_fitness.readonly` +
   `sleep.readonly`. Requires a fresh consent and a **new, health-only secret**
   (see gotcha 8 — do not add the scopes to `drive-oauth-token`).
2. Rotate the OAuth **client secret** (it was once pasted in chat).
3. Optional: archive scale screenshots to Drive as a provenance/bronze layer. They
   are deliberately *not* archived today — the transcribed numbers are the data,
   and the screenshot is still in the camera roll.
