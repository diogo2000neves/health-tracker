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
| 2 | **Biometrics / activity** | **Fitbit Air** (sleep, recovery, activity) | ✅ Built (~40 metrics/day) |
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

### Source 2 — Fitbit Air (sleep, recovery, activity)
The tracker syncs to **Google Health** in the background — no app to open, nothing
to tap — so unlike the scale, the API really is zero-touch here. The daily job
pulls it with a **health-only** OAuth token (`health-oauth-token`; see gotcha 8)
and fills ~40 columns per day. Scopes: `sleep.readonly`,
`health_metrics_and_measurements.readonly`, `activity_and_fitness.readonly`.

Two endpoints, chosen per data type (`src/google_health.py`):
* **`list`** for sleep sessions and the `daily-*` summaries (already one per day).
* **`dailyRollUp`** for everything intraday (steps, distance, calories, heart
  rate…). It aggregates server-side over **civil** days — our exact day grain —
  instead of us summing ~200 one-minute buckets. It is also the *only* route to
  some data: `floors` has no `list` endpoint at all, and **`total-calories`
  (calories OUT) exists only as a rollup**. Pair it with `total_cals_in` and you
  have true energy balance — the number this whole system was built to find.

**There is no sleep score, and there never will be.** Fitbit's 0-100 score is
proprietary and appears nowhere in the API (checked field-by-field against
discovery rev 20260715: the `Sleep` message is interval/type/metadata/summary/
stages only). Same story as the scale's body composition. What we store instead is
strictly *more* information — the components the score is computed from:
`sleep_efficiency_pct` (asleep÷in-bed, honest arithmetic), deep/REM/light minutes,
latency, awakenings, plus resting HR, HRV, SpO2, respiration and skin temperature.
Don't re-add a `sleep_score` column expecting the API to fill it.

**Naps are separated, and this matters.** A 2-hour afternoon nap ends on the same
*wake day* a night would land on, so left alone it silently overwrites that night.
`metadata.nap` routes it to `nap_mins` instead. If a night is fragmented into
several non-nap sessions, the longest is the night and the rest become naps, so the
sleep columns always describe one coherent sleep.

**Not produced by this device** (verified empty against the live API, not guessed):
floors and altitude (no altimeter), VO2 max / cardio fitness (needs a tracked run),
exercise sessions, blood glucose, core body temperature. ECG and irregular-rhythm
notifications sit behind their own extra scopes (`ecg`, `irn`) and were left
ungranted — least privilege, and the hardware almost certainly lacks the sensor.

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
              │ `daily_summary` : one row/day (sleep + recovery + activity +      │
              │                    nutrition + physique + self-report)            │
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
    Google Health API           Gemini API        Gemini API │    photo, a scale
    (Fitbit Air: sleep,         (insights)      (nutrition + │    screenshot, or a text
     recovery, activity)                         body OCR +  │    note that's a meal or
               │                                note router)  │    a bowel log; the model
    + rolls `meals` up into                                   │    routes it. POST /feel)
      the nutrition columns                          Google Drive (meal photos only)
```

- **Job vs Service:** the Service is a *push* endpoint that waits for the phone —
  meals, body composition and self-reports enter there, on one button. The daily
  Job *pulls* what a worn tracker syncs on its own. All scale to zero.
- **Why the daily job runs at 07:00**, two reasons: nutrition uses a **waking-day**
  grain and a day is only totalled once it is *over* (see below); and by then the
  night that just ended has been scored and synced, so it lands on its own wake-day
  row the same morning.
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
| Read Fitbit biometrics | **User OAuth token** — health scopes only (`health-oauth-token`) | health data needs *user* consent; a service account can't read it |
| Upload photos to Drive | **User OAuth token** — `drive.file` only (`drive-oauth-token`) | ⚠️ a service account has **zero Drive storage quota** — uploads must run as the user to use their 5 TB |
| Write the Sheet | **Service account** | the Sheet is shared with it as Editor |
| Call Gemini | **API key** | free tier |

Service account: `health-tracker-job@health-tracker-501322.iam.gserviceaccount.com`
(has no project-level roles; only resource-level secret access + run.invoker).

**Two user tokens, deliberately never merged** — the Health API 403s on any token
that also carries a Drive scope (gotcha 8). `src/auth.py` mints them separately
(`python -m src.authenticate health|drive`), into separate secrets. The health
token is read-only across `sleep`, `health_metrics_and_measurements` and
`activity_and_fitness`; nothing in this system ever writes back to Google Health.

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
| Secrets (Secret Manager) | `health-oauth-token` (health scopes only), `drive-oauth-token` (drive.file only), `ingest-token`, `gemini-api-key` |
| Code (master copy) | `/Users/dneves/Health Tracker/` — `src/` (job), `ingest/` (service) |

### Sheet schema
- **`daily_summary`** (78 columns), grouped by **who owns each block** — the
  merge-upsert means a source only ever writes its own columns:
  - `date`
  - **self-report** (ingest): `subjective_feel`, `bowel_movement`
  - **sleep** (Fitbit, wake-day): `sleep_start, sleep_end, time_in_bed_mins,
    sleep_mins, sleep_efficiency_pct, sleep_latency_mins, sleep_awake_mins,
    sleep_deep_mins, sleep_rem_mins, sleep_light_mins, sleep_awakenings, nap_mins`
  - **recovery** (Fitbit): `resting_hr_bpm, hrv_ms, hrv_deep_sleep_ms, hrv_entropy,
    non_rem_hr_bpm, spo2_pct, spo2_lower_pct, spo2_upper_pct,
    respiratory_rate_brpm, skin_temp_c, skin_temp_dev`
  - **activity** (Fitbit): `steps, distance_km, total_cals_out, active_cals,
    total_active_mins, active_mins_light/moderate/vigorous,
    azm_fat_burn_mins/azm_cardio_mins/azm_peak_mins, sedentary_mins,
    hr_min_bpm/hr_avg_bpm/hr_max_bpm, mins_hr_light/moderate/vigorous/peak,
    swim_strokes`
  - **nutrition** (meals roll-up): `total_cals_in, total_protein_g,
    total_carbs_g, total_fat_g` + **15 Tier-1 micronutrient totals**
    (`total_fiber_g, total_sugar_g, total_saturated_fat_g, total_sodium_mg,
    total_potassium_mg, total_calcium_mg, total_iron_mg, total_magnesium_mg,
    total_zinc_mg, total_vitamin_c_mg, total_vitamin_d_ug, total_vitamin_b12_ug,
    total_vitamin_a_ug, total_folate_ug, total_omega3_g`)
  - **body** (scale screenshot): `weight_kg, bmi, body_fat_pct,
    subcutaneous_fat_pct, visceral_fat, body_water_pct, muscle_mass_kg,
    bone_mass_kg, bmr_kcal, metabolic_age, lean_mass_kg, body_measured_at`
  - `updated_at`

  Column order lives in `src.sheets.DAILY_HEADERS`, composed from
  `src.biometrics.BIOMETRIC_COLUMNS` and `src.sheets.BODY_METRICS`. The scale
  metrics are **mirrored** in `ingest/main.py BODY_METRICS` (which also owns their
  plausibility bands) because ingest is a separate image and can't import `src`; a
  test asserts the two stay in step.

  Derived-and-stored (the sheet must stand alone for AI analysis):
  `sleep_efficiency_pct` = asleep÷in-bed · `skin_temp_dev` = nightly − 30-day
  baseline · `lean_mass_kg` = weight × (1 − fat%).
  - **Merge-upsert keyed on `date`** — each source fills only its own columns
    (scale screenshot → body, meals roll-up → nutrition, Fitbit → sleep/recovery/
    activity, /feel → subjective_feel, a bowel note → bowel_movement). Never
    overwrites a column it doesn't own.
    - ⚠️ **Exactly one row per date may reach `upsert_daily`.** It merges every
      row against the *same* pre-read grid snapshot, so two rows for one date make
      the second clobber the first's columns with stale values — or append the day
      twice if it's new. `run_daily.build_daily_rows` folds the biometric and
      nutrition column groups into one row per date for this reason.
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
   (`403 DISALLOWED_OAUTH_SCOPES`) — one token can never serve both. This is why
   there are two secrets (`health-oauth-token`, `drive-oauth-token`) and two
   profiles in `src/auth.py`. Never "simplify" them into one token.
9. **Google Photos API cannot read your library** (since Mar 2025) and **iCloud has
   no server API** — that's why photos are POSTed directly to our endpoint.
10. The sheet's **European locale renders decimals with commas** — every Sheets
    read must use `valueRenderOption="UNFORMATTED_VALUE"`, or `float("7,8")`
    silently zeroes the numbers. Avoid locale-sensitive formulas; charts and
    stats are written via the API instead.
11. **Read ranges must be wide enough for the schema.** `daily_summary` is 78
    columns; an `A1:Z` read silently truncates the header, so a column past the cut
    looks "missing" and its writes land nowhere. `sheets.READ_LAST_COL` is derived
    from `DAILY_HEADERS` — keep it that way, don't hard-code a letter.
12. A day is the **local civil day** (Europe/Lisbon), never the UTC day. Never
    `[:10]` a UTC timestamp to get it. Sleep intervals ship **no civil time** at
    all — only `startTime` + `startUtcOffset` — so the local day must be derived
    (a 23:03Z bedtime is already tomorrow in Lisbon).

### Google Health API specifics (all verified against the live API, 2026-07-16)

13. **`dataPoints.list` takes a `filter` expression, not `startTime`/`endTime`.**
    Four shapes: `{t}.date` (daily summaries), `{t}.sample_time.civil_time`,
    `{t}.interval.civil_start_time`, and **`sleep.interval.civil_end_time`** for
    sleep. The path is kebab-case, the filter field is snake_case. A wrong filter
    is a flat 400 — the old client caught that and retried *unbounded*.
14. **`dailyRollUp` caps its range**: **14 days** for `total-calories`,
    `heart-rate`, `active-minutes` and `calories-in-heart-rate-zone`; 90 for the
    rest. Over the cap is a 400. `google_health.daily_rollup` chunks automatically
    — a 7-day window hides this, so only a real backfill would ever expose it.
15. **Don't send `pageSize` to `dailyRollUp`.** It 400s on values the docs call
    legal (100 rejected for a 9-day range; 10 accepted). The 1440 default already
    dwarfs one-point-per-day. `sleep`/`exercise` cap `list` at **25**.
16. **Numbers arrive as JSON strings** (`"525"`, `"4151"`), durations as `"10620s"`,
    and — the nasty one — **an uncomputable metric comes back as the string
    `"NaN"`**. `float("NaN")` succeeds, so it passes every naive type check and
    lands as a literal NaN in the sheet (which strict JSON can't even serialise).
    `biometrics._num` rejects non-finite values for this reason.
    `baselineTemperatureCelsius` is `"NaN"` until 30 days of history exist, so
    `skin_temp_dev` stays blank until mid-August 2026 — that's expected, not a bug.
17. Some types have **no `list` endpoint at all** (`floors`: *"List is not
    supported… use reconcile, rollup, dailyRollup"*), and **`total-calories` exists
    only as a rollup**. Check the discovery doc
    (`curl 'https://health.googleapis.com/$discovery/rest?version=v4'`) rather than
    the HTML docs — it is authoritative, machine-readable and complete.

## 9. Open TODOs

1. Rotate the OAuth **client secret** (it was once pasted in chat).
2. Optional: archive scale screenshots to Drive as a provenance/bronze layer. They
   are deliberately *not* archived today — the transcribed numbers are the data,
   and the screenshot is still in the camera roll.
3. Optional: `ecg` + `irn` scopes, if the Air ever turns out to have the sensors.
4. `skin_temp_dev`, `azm_*` and `vo2_max` fill in as history accumulates — no code
   change needed, just don't mistake the blanks for a bug before then.
