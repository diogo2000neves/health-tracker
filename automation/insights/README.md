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
| `ingest/narrator.py` | The prompts — one for the whole feed, plus chat and memory extraction — and the Gemini transport. Prose only; never arithmetic, never food selection. |
| `ingest/coach_archive.py` | Append-only JSONL of every card, chat turn, event and report, sharded by month. Nothing is ever deleted or modified. |
| `ingest/coach_events.py` | The occasions — drinking, eating out, an outsized day — and the user's own meal notes. |
| `ingest/coach_recall.py` | Ranks the archive by relevance/recency/importance and clips each prompt section to a token budget. |
| `ingest/coach_reports.py` | The weekly / monthly / yearly rollups and their prompts. |
| `automation/coach/worker.py` | The Mac side: claims a job, answers it with Sonnet (or Opus for reports), posts it back. No nutrition logic. |

### Canonicalisation is what made food-level advice possible

The meal log is written by a vision model, so one food arrives under many names. Over
a real 28-day window that produced 116 "foods", 85 of them appearing exactly once —
`cod` / `boiled cod` / `boiled cod (bacalhau)`, `beef` / `beef steak` / `beef patty` /
`angus beef burger patty`. Every food-level rule read as zero. After canonicalisation
the same window is 75 foods with nothing unplaced, and "red meat seven times, fish
none" becomes a fact the coach can state.

## Which model writes it

Claude Sonnet, on the MacBook, is the primary. Gemini is the fallback. That ordering
is deliberate: the coaching voice *is* the product, and a flash model produces correct,
forgettable sentences where Sonnet notices that the oats held the morning and that
lunch was the tenth day running without anything green.

The backend never calls a model to generate the feed. It computes the facts, builds
the prompt, and parks it as a job:

```
POST /coach/generate     ->  writes coach/jobs/<id>.json, returns 202 "waiting_for": "sonnet"
GET  /coach/work         ->  the Mac worker claims the next job (a 15-minute lease);
                             a chat turn jumps the queue — someone is watching it
POST /coach/work/<id>    ->  {"answer": {...}}  the answer, validated and assembled
                             {"release": "..."} put it back — used for a spent usage window
POST /coach/sweep        ->  anything older than COACH_SONNET_WAIT_HOURS (5) goes to Gemini
```

So a sleeping laptop delays the coach; it cannot break it. An exhausted five-hour
usage window releases the job rather than consuming it, and the job keeps waiting for
Sonnet until the sweeper decides the wait has gone on long enough.

Both paths are given byte-identical prompts and pass through byte-identical
validation (`coach_feed.assemble`) — Sonnet gets no more benefit of the doubt than
Gemini. Each card records which model wrote it in `source`.

### Chat is queued work too

Conversation used to be the one synchronous path — the user is waiting, so a couple
of seconds seemed the right trade. It wasn't. The model call made the request slower
than the app's 60 s timeout, the app retried (it retries every POST), and the server
ran the whole turn again for each attempt: **one tap became three questions and three
different answers, all saved to history.**

So chat is a job like everything else. `POST /coach/chat` records the question,
parks it, and returns 202; Sonnet answers at **medium** effort (a chat turn needs the
voice, not the deep reasoning a weekly review gets); the app reads the answer out of
the thread whenever it next looks. Closing the app loses nothing.

The duplicate guard is the `client_turn_id` the app mints per message — not per HTTP
attempt — so a retry, a double tap or a background relaunch all collide with the same
id and are recognised as the same question. Moving the model out of the request path
alone would not have been enough: a retried POST would simply have queued two jobs.

The worker (`automation/coach/worker.py`) holds no nutrition logic at all: it claims
a job, runs `claude -p` with **no tools**, and posts the JSON back. The no-tools part
is not fussiness — the first live run had Read/Write available, did 238 s of good
work, wrote the JSON to a file and returned a prose summary, so the caller found no
answer at all. A prompt that needs no tools is given none.

## Memory

Everything the coach produces is kept, and almost none of it is in any given prompt.
Those two sentences are the whole design.

**Four tiers.** The *working* set (who this person is) is small, stable and always
present. The *episodic* archive (every card, conversation, event) is append-only and
never injected wholesale — it is queried. The *semantic* tier is the weekly/monthly/
yearly rollups, each summarising the level below. Retrieval assembles the last two
into a budget for each generation.

**Retrieval, not injection.** `coach_recall` scores memories the way the Generative
Agents memory stream does — relevance, recency, importance — with one deliberate
departure: relevance is exact topic-key overlap rather than embedding similarity.
Those systems need fuzzy matching because their memories are unstructured chat; these
carry typed keys (`alcohol`, `red_meat`, `swap_from:ham`, `finding:group_over:red_meat`),
and "what did you tell me last time I drank" is a structured query. That matters
because the [MemTier](https://arxiv.org/html/2605.03675) work found retrieval — not
model size — to be the binding constraint, measuring multi-session recall@2 at 0.038:
the needed memory was absent from the top results 96% of the time. Exact matching over
typed entries is, for this domain, close to their oracle.

**A budget, enforced.** Every section has a token allowance
(`coach_recall.BUDGET`), and items are dropped whole rather than truncated — half a
memory is worse than none, because the model can't tell which half is missing. A quiet
Tuesday retrieves almost nothing and costs almost nothing; a Friday with drinks in it
pulls up what happened last time and what was advised then.

**Hierarchy is what makes a year affordable.** A weekly report reads the week whole.
A monthly reads four or five weeklies. A yearly reads twelve monthlies. The prompt for
any report stays about a page no matter how much history exists, which is the
difference between keeping everything and drowning in it.

## Occasions, and the user's own words

The meal log carries a free-text note ("Comi um menu médio Big Tasty do McDonalds",
"Exagerei hoje") that the first version never showed the model at all — the items said
"burger, fries, iced tea" and the context the user had already supplied was thrown
away. `coach_events` now reads it, and detects the occasions averages destroy: a night
out is one event, not eight beers spread across a weekly mean.

Drinks are counted as *drinks*, not gram-servings. A 40 ml shot is a sixth of a 330 ml
beer by weight and the same thing socially, so serving-weight arithmetic scored eight
shots as a quiet evening.

## When it runs

Generation follows when the user actually eats, not the clock:

| trigger | when |
| --- | --- |
| a logged meal | schedules a run **one hour later**; another meal within that hour pushes it back, so the analysis lands when the meal is genuinely over |
| `coach-morning` | 07:30 — the day plan, before anything has been logged to trigger on |
| `coach-weekly` | Monday 10:00 — the week that just finished, on Opus |
| `coach-monthly` | 1st of the month, 11:00 — reads the weeklies |
| `coach-yearly` | 1 January, 12:00 — reads the monthlies |
| `coach-sweep` | every 30 min — hands anything Sonnet hasn't taken in five hours to Gemini |
| opening the app | only if the feed has nothing to say about the current part of the day |

The debounce is a Cloud Task scheduled an hour out carrying the timestamp of the meal
that scheduled it; when it fires it regenerates only if nothing has been logged since.
Identical jobs are deduplicated for 45 minutes, so opening the app repeatedly while
the Mac sleeps queues one job, not twenty.

## The feed is about *now*

Cards that describe a moment — `next_meal`, `check_in`, `day_summary` — are shown only
while the day is still in the part they were written for. Opening the app after dinner
leads with the day's whole story rather than the morning's read on breakfast, and the
feed reports itself stale so the app quietly asks for something current. Patterns,
wins and the weekly review are about habits and always stay.

## Endpoints

| method | path | notes |
| --- | --- | --- |
| `GET` | `/coach/feed` | The app's only read. Storage only — no model, no Sheets. Carries `stale` and `generating`. |
| `POST` | `/coach/refresh` | 202 + Cloud Tasks enqueue. Never blocks on a model. |
| `POST` | `/coach/generate` | Prepares a job (no model call). `{"slot": "morning\|afternoon\|evening\|weekly\|adhoc"}`. |
| `GET` | `/coach/work` | The Mac worker claims a job, or 204. |
| `POST` | `/coach/work/<id>` | The answer, or a release. |
| `POST` | `/coach/sweep` | Gemini takes over anything that waited too long. |
| `POST` | `/coach/chat` | Queues a question about one card for Sonnet. 202 + the transcript so far. **Idempotent on `client_turn_id`.** |
| `GET` | `/coach/thread/<id>` | One conversation, plus `pending` while an answer is still owed. |
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

Then the schedules. Only three are clock-driven — the rest follows the meals.

```bash
REGION=europe-west1
BASE=https://health-tracker-ingest-myznjtlyrq-ew.a.run.app
TOKEN=...   # the INGEST_TOKEN the service already holds

create() {  # name, cron, path, body
  gcloud scheduler jobs create http "coach-$1" --location=${REGION} \
    --schedule="$2" --time-zone="Europe/Lisbon" --uri="${BASE}$3" \
    --http-method=POST \
    --headers="Content-Type=application/json,X-Auth-Token=${TOKEN}" \
    --message-body="$4" --attempt-deadline=300s
}

create morning "30 7 * * *"  /coach/generate '{"slot":"morning","reason":"schedule"}'
create weekly  "0 9 * * SUN" /coach/generate '{"slot":"weekly","reason":"schedule"}'
create sweep   "*/30 * * * *" /coach/sweep   '{}'
```

Then the Sonnet worker on the Mac:

```bash
cp automation/coach/com.dneves.coach-worker.plist ~/Library/LaunchAgents/
# put the real INGEST_TOKEN in it first — launchd inherits no shell environment
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.dneves.coach-worker.plist
tail -f automation/coach/logs/worker.log
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
