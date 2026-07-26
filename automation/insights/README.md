# The Coach

The coach is a feed of small cards, each generated in the background and read by the
app as plain stored JSON. Nothing the app reads on this path calls a model.

## Why it is shaped this way

The first version generated on demand, cached its output in `/tmp` on Cloud Run, and
reasoned only over nutrient totals. All three choices broke it:

* **`/tmp` on a scale-to-zero service.** The cache was empty on nearly every open, so
  the app fell through to a 5–45 s Gemini call on the screen the user was looking at.
* **"Skipped" as a valid answer.** When no nutrient was below its floor, generation
  returned no plates at all, and the app's suggestion sheet sat on "preparing…"
  forever with nothing that could ever fill it.
* **Nutrient-shaped facts.** `ranked_issues` was a list of nutrient keys and food
  reached the model as 20 bare names, so the best it could produce was "eat more
  fibre" — which the Nutrients tab already says. Worse, asked for a food-level swap
  from that thin context, the model invented one; the critic pass caught it proposing
  *"swap your white bread"* for a log that contained no bread.

So: generation is scheduled and durable, the read path is a storage read, and the
facts lead with food.

## The pieces

| file | what it owns |
| --- | --- |
| `ingest/coach_store.py` | Durable JSON in Cloud Storage: cards per day, taxonomy, memory, chat threads, run state. Reads never raise; read-modify-write uses generation preconditions. |
| `ingest/food_taxonomy.py` | Canonical food names + nutritionist-grade groups. Curated rules first, one cached Gemini call for anything they can't place. |
| `ingest/food_patterns.py` | The deterministic reading of the log: servings/week per group vs reference, streaks, per-slot composition, variety, ranked findings with evidence, swap candidates. |
| `ingest/coach_feed.py` | Which cards a slot produces, dedup and cooldown, card ids/expiry/priority, and the validation that a swap references real logged food. |
| `ingest/coach_memory.py` | What the coach remembers between conversations: merge, dedup by meaning, bound, prune. |
| `ingest/narrator.py` | Gemini prompts: feed cards, plates, chat + memory extraction. Prose only — never arithmetic, never food selection. |

### Canonicalisation is what made food-level advice possible

The meal log is written by a vision model, so one food arrives under many names. Over
a real 28-day window that produced 116 "foods", 85 of them appearing exactly once —
`cod` / `boiled cod` / `boiled cod (bacalhau)`, `beef` / `beef steak` / `beef patty` /
`angus beef burger patty`. Every food-level rule read as zero. After canonicalisation
the same window is 75 foods with nothing unplaced, and "red meat seven times, fish
none" becomes a fact the coach can state.

## Endpoints

| method | path | notes |
| --- | --- | --- |
| `GET` | `/coach/feed` | The app's only read. Storage only — no model, no Sheets. Carries `stale` and `generating`. |
| `POST` | `/coach/refresh` | 202 + Cloud Tasks enqueue. Never blocks on a model. |
| `POST` | `/coach/generate` | The worker. `{"slot": "morning\|afternoon\|evening\|weekly\|adhoc"}`. One run at a time; retries replace their own cards. |
| `POST` | `/coach/chat` | A turn in the conversation about one card; also folds anything durable into memory. |
| `GET` | `/coach/thread/<id>` | One conversation. |
| `GET`/`POST`/`DELETE` | `/coach/memory[/<id>]` | Read, add to, and correct what the coach remembers. |
| `GET` | `/coach/patterns` | The deterministic analysis, unnarrated — eyeball this before spending a model call. |

The `/insights/*` endpoints are unchanged and still serve the older app build.

## Setup (one time)

The service needs a bucket to write to. `COACH_BUCKET` is the only new env var, and
without it the coach falls back to a local directory — which is why the tests need no
credentials.

```bash
PROJECT=health-tracker-501322
REGION=europe-west1
BUCKET=health-tracker-coach
SA=health-tracker-job@${PROJECT}.iam.gserviceaccount.com

gcloud storage buckets create gs://${BUCKET} --project=${PROJECT} --location=${REGION}
gcloud storage buckets add-iam-policy-binding gs://${BUCKET} \
  --member=serviceAccount:${SA} --role=roles/storage.objectAdmin
gcloud run services update health-tracker-ingest --region=${REGION} \
  --update-env-vars=COACH_BUCKET=${BUCKET}
```

Then the four generation slots. Cloud Scheduler calls the worker directly; the times
are in the service's own timezone (`Europe/Lisbon`).

```bash
REGION=europe-west1
URL=https://health-tracker-ingest-myznjtlyrq-ew.a.run.app/coach/generate
TOKEN=...   # the INGEST_TOKEN the service already holds

create() {  # name, cron, slot
  gcloud scheduler jobs create http "coach-$1" --location=${REGION} \
    --schedule="$2" --time-zone="Europe/Lisbon" --uri="${URL}" \
    --http-method=POST \
    --headers="Content-Type=application/json,X-Auth-Token=${TOKEN}" \
    --message-body="{\"slot\":\"$3\",\"reason\":\"schedule\"}" \
    --attempt-deadline=300s
}

create morning   "30 7 * * *"  morning
create afternoon "30 15 * * *" afternoon
create evening   "30 21 * * *" evening
create weekly    "0 9 * * SUN" weekly
```

A meal landing also enqueues an `adhoc` refresh (see `_finalize` in `ingest/main.py`),
so the "what to eat next" card follows the day rather than the clock.

### Verify

```bash
BASE=https://health-tracker-ingest-myznjtlyrq-ew.a.run.app
curl -s -H "X-Auth-Token: $TOKEN" "$BASE/coach/patterns" | jq '.findings[].headline'
curl -s -X POST -H "X-Auth-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"slot":"afternoon"}' "$BASE/coach/generate"
curl -s -H "X-Auth-Token: $TOKEN" "$BASE/coach/feed" | jq '.cards[] | {kind, title}'
```

## The local Mac job is retired

`generate.py` and the `com.dneves.insights-*` launchd agents were the original
generation path. They had been failing every run since 2026-07-24 with
`GEMINI_API_KEY is not configured` — launchd doesn't inherit a shell environment — so
the weekly report the app showed was frozen at 2026-07-23 and the `next_meal` sheet tab
was empty. Generation now runs on Cloud Run, where the key already lives, and the
agents are unloaded:

```bash
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.dneves.insights-weekly.plist
launchctl bootout gui/$UID ~/Library/LaunchAgents/com.dneves.insights-nextmeal.plist
```

The plists are left in place, so re-enabling is `launchctl bootstrap gui/$UID <plist>`.

`generate.py` stays as a manual debug tool for the legacy weekly report:

```bash
export GEMINI_API_KEY=... HEALTH_BACKEND_URL=... INGEST_TOKEN=...
backend/venv/bin/python automation/insights/generate.py weekly --dry-run
```

## Safety

Diet advice to someone in a deficit chasing recomposition. The prompts forbid medical
claims and any "eat less for its own sake" framing, and the enforcement that matters is
in code, not in the prompt:

* a swap's `from` must be a food that appears in the log, and its `to` must be one of
  the options the finding offered (`coach_feed._validated_swap`);
* a `pattern` card must reference a real finding, or it is dropped;
* no finding fires below `MIN_DAYS_FOR_FINDING` logged days — with a thin log, "no fish
  this week" is a gap in the log, not a gap in the diet;
* group reference ranges come from mainstream dietary guidance and are stated as
  observations about what was logged, never as diagnoses.

Memory is bounded, listed in the app, and deletable, because a wrong inference that
silently shapes every future card is worse than no memory at all.
