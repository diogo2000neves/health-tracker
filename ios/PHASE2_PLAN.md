# Phase 2 — Weekly Insights & Next-Meal Coach

The app currently *records* nutrition. Phase 2 makes it *advise* — a weekly coaching
review plus an on-demand "what should I eat next" answer, both built on a deep,
deterministic analysis of the last 7 days and grounded in the foods the user actually
eats. The goal is advice a person will act on, not a report they read once and forget.

This doc is the build contract. It mirrors the house rules already in the repo:
deterministic computation is separated from model reasoning; the nutrition *science*
lives in exactly one place; observation tabs are never rebuilt; the strong local model
does the writing.

---

## 1. The hard boundary

Five stages, one rule: **the model never does arithmetic.** Everything left of the
boundary is pure code, unit-tested with pytest. The model only ever receives finished
facts and turns them into words.

```
meals (items JSON) ─┐
targets / basis ────┼─► [A] AGGREGATE ─► [B] JUDGE ─► │ ─► [C] NARRATE ─► [D] CRITIC ─► weekly_reports
food_profile ───────┘   (deterministic, backend)      │    (strong model, local Mac)     (immutable)
                                                        │
today consumed/targets ─► [E] NEXT-MEAL CONTEXT ────────┘─► plate assembly ─► next_meal (cache)
```

- **A + B (the Diagnosis) run in the backend** (`ingest/insights.py`), reusing the
  existing nutrient science (`NUTRIENT_KEYS`, `_NUTRIENT_KINETICS`, the resolved
  targets). This keeps a single source of truth for the science and gives the
  deterministic core full pytest coverage. Exposed as read-only JSON.
- **C + D + E run on the local Mac** (`automation/insights/`), using the subscription
  `claude` CLI (the strongest model, same transport as the audit). It fetches the
  Diagnosis, writes the narrative + plates to Sheets. If the Mac is offline the app
  serves the last good report — generation waits.
- **The app only ever reads cached Sheet rows** through the backend. No model call is
  ever on the request path.

## 2. Data model

Three new tabs + one config file. The important distinction is *which kind* each is.

| artifact | kind | write discipline | why |
| --- | --- | --- | --- |
| `weekly_reports` | **observation** | upsert on `week_start`, **never rebuilt** | continuity depends on last week's report being the exact snapshot the user read. Rewriting it makes "you improved since I flagged X" a lie. |
| `food_profile` | derived | `replace_tab` each run | pure function of `meals`; always fresh, inspectable in the sheet. |
| `next_meal` | derived cache | `replace_tab` (keyed by date) | on-demand suggestion; disposable. |
| `nutrient_policy.json` | config | in repo, version-controlled | the "genuine issue vs non-problem" rules. Not data. |

### weekly_reports columns
`week_start` · `generated_at` · `window_start` · `window_end` · `diagnosis_json`
(the Layer-A facts) · `report_json` (the Layer-C narrative) · `focus_key` ·
`focus_value` (denormalized so next week computes the delta without parsing) ·
`prior_focus_key` · `prior_focus_delta` · `coverage_note` · `model` · `critic_verdict`
· `status` (`generated | superseded`).

### next_meal columns
`date` · `generated_at` · `snapshot_json` (day-so-far: calories/protein left, low
nutrients) · `focus_key` · `plates_json` (3 ranked plates) · `model` · `status`.

## 3. nutrient_policy — the deep analysis, made mechanical

A JSON keyed by nutrient. A raw deficit only becomes a *genuine issue* after passing
this gauntlet, so the model is never handed a false alarm.

- **`goal_weight`** (0–1) — importance for the recomp goal. Protein `1.0`, biotin
  `0.15`. Ranks which issue leads the week.
- **`excess_posture`** — `none | note | flag | hard_flag`. Cholesterol `none`
  (track-only; dietary cholesterol is a weak LDL predictor for most people), saturated
  fat `flag`, trans fat `hard_flag`. Stops the "you spiked cholesterol!" false alarm.
- **`deficit_from_food`** — `strong | weak`. Vitamin D `weak` (skin/sun is the real
  source, so a low food figure is a *note*, not a deficiency), most others `strong`.
- **`coverage_floor`** — minimum fraction of the window's meals that must carry data
  for the nutrient before we say anything. Below it → `unknown`, never `deficit`. This
  is the missing-data guard: a gap in extraction must not read as a gap in the diet.

`horizon` (daily vs rolling) is **not** duplicated here — it already lives on the
target and is read from there, so a rolling nutrient (B12, iron) is judged on the
window average and a daily one (magnesium, C) on consistency.

## 4. The Diagnosis object (A→C contract, and what `/insights/diagnose` returns)

```jsonc
{
  "window": { "start", "end", "days_logged", "meals_logged" },
  "adherence": { "calories": {...}, "protein_g": { "mean", "target", "pct", "days_hit", "per_kg" }, ... },
  "nutrients": [{
    "key", "horizon", "mean", "target", "pct", "coverage",
    "status",         // deficit | adequate | over | over_benign | unknown | approaching_ul
    "genuine_issue",  // true | false | "weak"
    "trend",          // improving | declining | steady | null (vs prior window)
    "goal_weight",
    "attribution": [{ "food", "amount", "pct" }],   // top contributors, real foods
    "note"            // policy note, e.g. the vitamin-D / cholesterol caveat
  }],
  "ranked_issues": ["saturated_fat_g", "omega3_g", ...],   // goal-weighted, genuine only
  "wins": [{ "kind", "key", "detail" }],
  "correlations": [],                                       // pre-registered, added later
  "coverage_note": "..."
}
```

## 5. Stages C / D / E (local, strong model)

- **C — Narrate.** Input: Diagnosis + food_profile + last week's focus & its measured
  delta. Output: **structured JSON** (`headline`, `wins[]`, `focus{key, why,
  attribution_sentence}`, `swap{from, to, why}`) — never prose the app has to parse, so
  the UI renders native components. pt-PT, warm, one focus only.
- **D — Critic.** A second call validates the draft against the Diagnosis: every claim
  traceable to a number, alarm level matches `excess_posture`, no medical claims, no
  restrictive/disordered framing (celebrate adequacy, never reward eating less). Fail →
  regenerate.
- **E — Next-meal.** Deterministic portion math (`grams = clamp(gap / density,
  min_serving, min(max_serving, calorie_budget))`, emitted as a **range**) sets the
  quantities; the model only picks palatable, real foods and assembles **3 ranked
  plates** (first = recommended). A *novelty budget* allows ≤1 new healthy food —
  either a better neighbour in a category the user eats, or filling a category they have
  none of — never an unpalatable "nutrient bomb".

## 6. Endpoints

Read-only, all behind the existing API-key auth. No model runs in Cloud Run.

- `GET /insights/diagnose?date=YYYY-MM-DD` — the Diagnosis JSON (debug + the local
  job's data source).
- `GET /insights/food-profile` — the derived vocabulary.
- `GET /insights/next-meal-context` — today's remaining budget + candidate densities.
- `GET /insights/weekly` — the latest cached weekly report (app).
- `GET /insights/next-meal` — the latest cached plates (app).

## 7. iOS

A new `InsightsView` tab: the Sunday review (continuity strip → headline → wins → the
one focus with its attribution sentence → the swap), and an always-available next-meal
sheet with 3 ranked plates and tap-to-log. Native components rendered from the
structured JSON; reuses `NutrientCatalog` for labels/units. pt-PT throughout.

## 8. Build order & status

1. `nutrient_policy.json` + `ingest/insights.py` (Diagnosis, food_profile, portion
   math) + `tests/test_insights.py`. ← the deterministic core, the differentiator.
2. Backend read-only endpoints (`/insights/*`).
3. `automation/insights/` local generator (narrate + critic + next-meal) + Sheets writes.
4. iOS `InsightsView` + models + API + tab wiring.
5. Evals: an actionability rubric, and the advice→outcome loop (free from
   `weekly_reports`).

## 9. Guardrails

Diet advice to someone in a deficit chasing recomposition — the exact profile where a
tracker can nudge toward disordered patterns. No medical claims (suggest a clinician,
never diagnose); adequacy is the framed win; the critic enforces both. Nothing is ever
committed/pushed automatically — a push deploys to Cloud Run, so shipping stays a human
decision.
