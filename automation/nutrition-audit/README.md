# Nutrition audit — multi-model reconciliation + database grounding

A **local** job (this MacBook) that upgrades the cloud pipeline's single Gemini
estimate of each meal into a far more accurate one, by treating meal analysis as the
**two different problems it actually is**:

- **Perception** — *what is on the plate and how many grams* (identity, portions,
  hidden fats). Different models genuinely disagree here, and the disagreement is
  useful, so we **ensemble**: Gemini's estimate (already in the row) + a fresh,
  independent Claude estimate, **reconciled against the photo** — not one overwriting
  the other.
- **Knowledge** — *the ~30 micronutrient values for a known food × grams*. That's a
  lookup, not a guess, and a model reciting it from memory is noisy. So we **ground**
  it in **USDA FoodData Central (FDC)**: measured, deterministic, comparable across
  days. The model's estimate is kept only for the handful of keys FDC lacks.

```
   Gemini(row)  +  Claude estimate   →   adjudicate   →   ground (FDC)   →   write
   └─ independent perception opinions ─┘   └ reconcile ┘   └ knowledge lookup ┘
```

It lives here (not in `backend/`) because it uses the local `claude` CLI on the
personal Claude subscription, which only exists on this machine. It does **not** touch
any cloud code — it only reads and writes the same Google Sheet the cloud services
already share. Runs at **11:00, 15:00, 23:00** daily.

## The pipeline, stage by stage

1. **Select** the day's meals to audit — logged **today** (Europe/Lisbon), **has a
   photo**, is a real meal (not a `not food` / `analysis failed` stub), is **not a
   template** (kitchen-scale ground truth — never overwritten), and **not already
   audited**.

2. **Gather independent estimates** (`estimate.py`). Estimate #1 is Gemini's, already
   in the row from ingest — free, no API call. Estimate #2 is a fresh Claude pass that
   is **never shown any other model's numbers** (see *Why the estimate is independent*).
   A **disagreement-gated third estimator** (e.g. Gemini 3.1 Pro) can plug in at
   `audit._THIRD_ESTIMATOR` and is called **only** when the first two diverge past
   `AUDIT_THIRD_MODEL_DISAGREEMENT` (default 25 %) — spend the extra opinion where the
   uncertainty is, not on every meal.

3. **Adjudicate** (`adjudicate.py`) — the step that replaces the old "second model
   overwrites the first" coup. A fresh Claude pass looks at the photo again and
   reconciles the estimates item-by-item, with explicit rules:
   - **median for magnitudes** — agree on grams → take consensus; diverge → adjudicate
     from the pixels;
   - **union for omissions** — hidden oil/sauce/sugar are errors of omission, so if
     **any** estimate flagged one and it's plausible, keep it;
   - **no default winner** — estimates are shown **blind** (labelled A/B, model identity
     stripped, order shuffled) so it resolves on evidence, not on which model it was;
   - **note already applied** — every estimate already applied the note once, so the
     adjudicator must not re-apply "half"/"double" (avoids the double-count trap).

   With only one usable estimate (e.g. no Gemini row items) it skips straight to
   grounding that estimate.

4. **Ground** (`ground.py` + `fdc.py`) — for each reconciled item: search FDC, let a
   **light** model call pick the best-matching entry (or decline — a wrong match is
   worse than none), pull its measured **per-100 g** panel, scale to the grams, and
   **merge over the model estimate**: FDC wins for every key it supplies (~30 / 36 for
   a whole food); the model's value is kept for the keys FDC omits (added sugar,
   chloride, iodine, biotin). **Macros stay the vision estimate** (how much protein/fat
   is on *this* plate is perception), but the **fat-composition split** (sat/mono/poly/
   omega) is taken from FDC's ratios and **rescaled to the vision total fat** — "what
   kind of fat" from the database, "how much" from the eyes.

5. **Write** the same `meals` row shape back (totals re-summed from items in code), and
   **stamp `model`** with `claude-audit:<model> | was:<original>` so the meal is audited
   once. **Confidence is lowered when the estimates disagreed** (Phase 4) — the
   disagreement is surfaced through the confidence the app already shows, not just
   logged.

6. **Log** to the audit-owned **`meal_reviews`** tab — one row per meal, laid out
   left-to-right as the whole story of the review (built for transparency while
   testing):

   | column | what it shows |
   |---|---|
   | `reviewed_at` / `duration_s` | when the review ran and how long this meal took |
   | `datetime` / `foods` | the meal (cross-refs the meals row) and its final food list |
   | `stage` | `adjudicated` / `single-estimate` / `fallback-estimate` / **`skipped`** |
   | `models` | which models took part + the adjudicator, with their ids |
   | `gemini_said` | **Gemini's own conclusion** (the ingest estimate): kcal, macros, portion, item + nutrient-key counts |
   | `claude_said` | **the independent Claude estimate's conclusion**, same format |
   | `third_said` | **Gemini 3.1 Pro's conclusion**, or a clear reason it wasn't invoked (agreement below gate / not configured) |
   | `disagreement` | how far the estimates diverged — the ensemble signal |
   | `adjudicator_verdict` | **what the adjudicator decided**, per item: agreed / adjudicated / added, and why |
   | `grounding` | **per-item nutrient source** — which FDC entry backed each item, or that the model estimate was kept (and why) |
   | `final` | **the final verdict** written to the meal: totals + confidence |
   | `delta` | before(Gemini)→after: calories, protein, nutrient-key count |
   | `review_notes` | the adjudicator's own reasoning |
   | `image_sha` | upsert key (a re-review updates its row, never duplicates) |

   To see what a review did, read this tab; to see *whether* a meal was reviewed at a
   glance, look at the meals `model` column. **The same breakdown prints in the
   `--dry-run` log**, so you can inspect a meal without writing anything. If the schema
   changes, the tab is cleared and rewritten on the next run (audit-owned data — re-run
   with `--force` to repopulate).

   **When the audit can't run** (Claude at its 5-hour usage cap, a photo download
   failure, an unparseable answer), the meal's own row is left untouched — the original
   Gemini estimate stands — and a **`skipped`** row is written to `meal_reviews` naming
   the reason (e.g. `Claude estimate failed (likely usage cap): ...`), with `final` =
   *KEPT ORIGINAL Gemini estimate — not audited*. So a capped run is **visible**, not
   silent. The meal isn't stamped audited, so the next scheduled run retries it; when it
   succeeds, the real review overwrites the skip row (upsert on `image_sha`). A failed
   `--force` re-audit never overwrites an already-good review.

**Why the estimate is independent (important).** A note like *"I ate two of this"* is
an instruction **relative to the photo**. Gemini may or may not have already applied it
(its handling is inconsistent). If Claude were shown Gemini's numbers *and* the note it
could apply the note a **second time** and double-count. The photo is invariant — it
always shows the base portion — so each estimate applies the note **exactly once**, and
only the adjudicator combines them (told explicitly the note is already applied). This
removes anchoring bias and is why the estimates are blinded from each other.

**Safety by construction.** A failed/unparseable estimate or adjudication **leaves the
original row untouched**, and any stage degrades gracefully: adjudication failure → the
independent Claude estimate (the old single-model behaviour); FDC rate-limit or error →
the model's own micros for the affected items. A real meal is never zeroed and a bad
call never corrupts data. The target row is re-located by `image_sha` immediately before
writing, so concurrent cloud writes can't cause a wrong-row update.

## Files

| File | Purpose |
|---|---|
| `audit.py` | Orchestrator + Sheets/Drive plumbing + `meal_reviews` log. Run by launchd; also runnable by hand. |
| `estimate.py` | One independent estimate from the photo + note (Layer A + macros + micros). Doubles as the safe fallback. |
| `adjudicate.py` | Phase 1 — reconcile the blinded independent estimates against the image. |
| `fdc.py` | USDA FoodData Central client + the verified nutrient-number → our-36-keys mapping (cached to `logs/fdc_cache.json`). |
| `ground.py` | Phase 2 — match items to FDC and merge the measured panel over the model estimate. |
| `nutrients.py` | Shared nutrient schema + normalization (the exact item shape ingest writes). |
| `claude_cli.py` | Thin wrapper around the headless `claude` CLI returning parsed JSON. |
| `eval_templates.py` | Phase 0 — measure the pipeline against the kitchen-scale templates. |
| `authorize.py` | One-time Google OAuth consent (Sheets + Drive). |
| `com.dneves.nutrition-audit.plist` | launchd schedule (source of truth; a copy is installed in `~/Library/LaunchAgents/`). |
| `logs/` | Run logs + FDC cache + temp dir. **Git-ignored.** |

Auth token: `backend/credentials/token_nutrition_audit.json` (git-ignored). Uses the
same OAuth client as the rest of the system but its own token with `spreadsheets` +
`drive.file` scopes.

## Configuration

| Env var | Default | What it does |
|---|---|---|
| `FDC_API_KEY` | `DEMO_KEY` | USDA key. **DEMO_KEY is capped at ~30 req/hour** — get a free key (instant, no billing) at <https://fdc.nal.usda.gov/api-key-signup.html> and export it to lift the cap to ~1000/hour. |
| `FDC_MATCH_MODEL` / `FDC_MATCH_EFFORT` | `sonnet` / `low` | Model + effort for the light FDC-matching call (text ranking — cheap). |
| `AUDIT_THIRD_MODEL_DISAGREEMENT` | `0.25` | Divergence above which the third estimator is invoked. |
| `GEMINI_BIN` / `GEMINI_CLI_MODEL` | `gemini` / `gemini-3.1-pro-preview` | The third estimator's CLI binary + model (see below). |
| `AUDIT_CLAUDE_TIMEOUT_S` *(via each stage)* | `900` | Per-call timeout for the heavy image estimate/adjudication calls. |
| `HEALTH_SPREADSHEET_ID`, `HEALTH_TZ`, `CLAUDE_BIN` | see code | Overrides the backend also honours. |

### The third model (Gemini 3.1 Pro)

`gemini_estimate.py` shells out to the local **`gemini` CLI** — subscription-backed,
exactly like the `claude` CLI, **no API key**. It auto-wires when the `gemini` binary
is on PATH (`audit.py` checks `gemini_estimate.available()`), and stays off otherwise.
It reuses the identical independent-estimate prompt, so Gemini and Claude answer the
same question; it hands images to the CLI as `@<path>` refs and runs
`--approval-mode yolo` so an unattended run never blocks on a tool prompt. If your CLI
uses different flags/model ids, override `GEMINI_BIN` / `GEMINI_CLI_MODEL` or adjust the
argv in `gemini_estimate.py`. (The CLI must be logged in to your subscription.)

## Running it by hand

Always use the backend venv's Python (it has the Google libraries):

```bash
cd /Users/dneves/personal/health-tracker
PY=backend/venv/bin/python

# verify Sheets read + one photo download + FDC reachability
$PY automation/nutrition-audit/audit.py --check

# see what WOULD change, writing nothing (default: today)
$PY automation/nutrition-audit/audit.py --dry-run
$PY automation/nutrition-audit/audit.py --dry-run --date 2026-07-15 --limit 1

# live: write the revised rows
$PY automation/nutrition-audit/audit.py
$PY automation/nutrition-audit/audit.py --date 2026-07-15 --limit 1
```

Flags: `--check`, `--dry-run`, `--date YYYY-MM-DD` (default today), `--limit N`,
`--force` (re-review already-audited meals).

## Measuring accuracy (Phase 0)

The whole strategy is falsifiable because the **templates are kitchen-scale ground
truth**. Run the eval on template-matched meal photos to see, in numbers, whether
adjudication beats a single estimate and whether grounding pulls the micros toward the
database:

```bash
$PY automation/nutrition-audit/eval_templates.py --limit 3
```

It prints mean relative macro error (**single vs adjudicated**, vs the scale) and mean
micro error (**model vs FDC-grounded**, vs the templates grounded to FDC), and writes
the raw per-meal JSON to `logs/`. Each meal costs ~3 heavy Claude calls, so keep
`--limit` small — it's a calibration tool, not a daily job.

## Cost / usage

Per meal: one independent Claude estimate + one adjudication (both **sonnet, high
effort**, image, ~6–9 min each) + one **light** FDC-matching call + a handful of cached
FDC HTTP lookups. Roughly **2 heavy calls/meal** (vs 1 before), all background, plus a
third heavy call only on high-disagreement meals once that estimator is wired. FDC is
free; the on-disk cache means a repeated food costs nothing.

## Schedule — disable / re-enable

```bash
launchctl print gui/$(id -u)/com.dneves.nutrition-audit                       # status
launchctl bootout gui/$(id -u)/com.dneves.nutrition-audit                     # disable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dneves.nutrition-audit.plist   # re-enable
launchctl kickstart -k gui/$(id -u)/com.dneves.nutrition-audit               # run now (respects audit-once)
```

To change the times: edit the plist here, copy it to `~/Library/LaunchAgents/`, then
bootout + bootstrap. If the Mac is asleep at a scheduled time, launchd runs the missed
job shortly after it next wakes.

## Re-authorizing

If the token is revoked/expired and can't refresh:

```bash
backend/venv/bin/python automation/nutrition-audit/authorize.py
backend/venv/bin/python automation/nutrition-audit/audit.py --check
```

## Troubleshooting

- **Timeouts** — a high-effort call is slow (per-call limit 15 min, `AUDIT_CLAUDE_TIMEOUT_S`).
  A timed-out meal is skipped, original untouched, picked up next run.
- **Parse failures** — the raw model answer is saved to `logs/tmp/last_parse_failure.txt`.
- **FDC 429 / DEMO_KEY** — grounding falls back to model micros and logs it; get a free
  `FDC_API_KEY` to stop hitting the cap.
- **Nothing audited** — check the day has photo meals not already audited: `--dry-run
  --date <day>`. Rows carrying a `claude-audit` model are skipped by design.
- **Logs** — `logs/audit.log` (one line per meal: stage, old→new macros, disagreement,
  grounding coverage), plus `logs/launchd.out.log` / `logs/launchd.err.log`.
