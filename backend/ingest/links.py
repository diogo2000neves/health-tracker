"""Cross-domain links: what actually leads to what, in this person's own data.

This is the part a human genuinely cannot do by eye — noticing that the nights
following a late, fat-heavy dinner carry twenty minutes less deep sleep, across
three months of days that all felt the same at the time.

## The model does not find the links. It explains them.

Handing a language model ninety rows of eighty columns and asking what it notices
produces fluent, confident, *invented* correlations — and a false chain is worse
than no chain, because the user acts on it. There are ~3,000 column pairs in this
schema; at p<0.05, pure noise yields ~150 "significant" findings. That is not a
model limitation, it is multiple comparisons, and no amount of prompting fixes it.

So the engine here finds candidates and the model's only job is to explain the one
that survived and turn it into an action. This is the same rule
`coach_feed._validated_swap` already enforces for foods, extended: **the model may
not claim a link the engine did not find.**

## Declared hypotheses, not discovery

`LINKS` is a hand-written table of physiologically plausible cause/effect pairs,
each carrying its mechanism in one sentence. Curated beats automated at this sample
size: it is honest about multiple comparisons, it is reviewable, it fails loudly
rather than silently, and the mechanism is what lets a card say *why* instead of
just *that*. Adding a hypothesis is one entry.

## The lag comes from the schema, not from the hypothesis

The subtle part, and the reason this module is short. A row is an observation of a
date, not a causal unit. Within row N the true order is:

    sleep/recovery (the night that ENDED that morning)
      -> the weigh-in (that morning, fasted)
        -> food and activity (during the day that follows)

So food on row N meets its night on row **N+1**, while poor sleep on row N precedes
the eating on that **same** row. Hand-writing a lag per hypothesis would reintroduce
exactly the off-by-one the registry exists to prevent, so `_offset` derives it from
each column's declared `causal` window instead. Get this wrong and every finding
here silently asks whether tomorrow's dinner affected last night's sleep.

## The statistics, and why they are the ones they are

* **A tertile contrast, not a correlation coefficient.** "-18 min of deep sleep on
  the nights after your heaviest late dinners" is a sentence; r = -0.31 is not. It
  is also more robust to the outliers this data is full of.
* **A permutation test**, because it is exact, assumption-free and pure stdlib — no
  t-distribution to import, and no normality assumption these metrics would honour.
* **Benjamini-Hochberg across everything evaluated in the run.** Testing ~25
  hypotheses at p<0.05 buys ~1 false positive per run by construction; FDR control
  is the cheap, correct fix, and without it this module would confidently invent a
  link every few days.
* **A stability check**: the effect must point the same way in both halves of the
  window. A link driven entirely by one strange fortnight does not survive it.
* **A minimum n per hypothesis**, below which it is not evaluated at all.

Everything is a *contrast*, never a claim of causation; `direction_is_expected`
records only that the observed sign matched the declared mechanism, and the card
copy is required to say "associado a", never "causa".

Pure stdlib, and deterministic given a seed, so every rule here is unit-testable.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Causal windows, mirrored from schema/registry.py. Duplicated as bare strings for
# the same reason food_patterns duplicates the taxonomy: this module is flattened
# next to main.py in the container image, while the registry lives in a package.
# `_offset` asserts the pairing rules rather than the strings, and a test pins these
# against the registry so the two can never drift apart.
WAKING_DAY, CALENDAR_DAY = "waking_day", "calendar_day"
NIGHT_ENDING, MORNING_OF, DAY_OF = "night_ending", "morning_of", "day_of"

CAUSAL_INPUT = frozenset({WAKING_DAY, CALENDAR_DAY})
CAUSAL_OUTCOME = frozenset({NIGHT_ENDING, MORNING_OF, DAY_OF})

UP, DOWN = "up", "down"

# How many permutations back each p-value. 2000 gives a resolution of 0.0005 — far
# finer than the 0.05 gate — and the whole table costs a few milliseconds on ~90
# rows, so there is nothing to buy by lowering it.
PERMUTATIONS = 2000
ALPHA = 0.05
# Deterministic by default: the same window must produce the same links on a re-run,
# or a card could appear and vanish between two refreshes of the same day.
SEED = 20260727


@dataclass(frozen=True)
class Feature:
    """A per-day number that isn't a `daily_summary` column — computed from the meal
    rows, which carry the timestamps and per-item detail the daily roll-up flattens
    away. Declares its own causal window so `_offset` can treat it exactly like a
    column."""
    name: str
    causal: str
    label_pt: str
    unit: str = ""


# Meal-derived features. All are WAKING_DAY: they describe food, and food is keyed
# on the waking day (05:00 to 05:00) exactly like the nutrition columns.
FEATURES: Dict[str, Feature] = {
    f.name: f for f in (
        Feature("last_meal_hour", WAKING_DAY, "hora da última refeição", "h"),
        Feature("calories_after_21h", WAKING_DAY, "calorias depois das 21h", "kcal"),
        Feature("fat_g_after_21h", WAKING_DAY, "gordura depois das 21h", "g"),
        Feature("meal_count", WAKING_DAY, "número de refeições", ""),
        Feature("largest_meal_kcal", WAKING_DAY, "maior refeição do dia", "kcal"),
        Feature("protein_at_breakfast_g", WAKING_DAY,
                "proteína ao pequeno-almoço", "g"),
    )
}


@dataclass(frozen=True)
class Link:
    """One declared hypothesis.

    `mechanism` is not decoration: it is what turns "these two moved together" into
    something a person can act on, and it is handed to the model as the explanation
    it must stay within. `blocks` is the capability gate — a link is only evaluated
    when the user actually measures both sides.
    """
    id: str
    cause: str
    effect: str
    expect: str                 # UP or DOWN — which way `effect` should move
    blocks: Tuple[str, ...]     # every schema block this link needs
    mechanism: str              # pt-PT, one sentence
    label_pt: str               # the headline, pt-PT
    min_n: int = 20
    min_effect: float = 0.0     # in the effect's own units; 0 = any real effect


# The hypothesis table. Each entry is a claim someone could argue with, which is the
# point — this is where the domain knowledge lives, in version control, next to a
# test suite, rather than inside a prompt.
LINKS: Tuple[Link, ...] = (
    # -- food -> sleep --------------------------------------------------------
    Link("late_calories_deep_sleep", "calories_after_21h", "sleep_deep_mins", DOWN,
         ("nutrition", "sleep"), min_n=20, min_effect=8,
         label_pt="comer tarde e sono profundo",
         mechanism="A digestão de uma refeição tardia mantém a temperatura central "
                   "elevada nas primeiras horas de sono, que é precisamente onde "
                   "vive o sono profundo."),
    Link("late_fat_sleep_latency", "fat_g_after_21h", "sleep_latency_mins", UP,
         ("nutrition", "sleep"), min_n=20, min_effect=5,
         label_pt="gordura à noite e demorar a adormecer",
         mechanism="A gordura atrasa o esvaziamento gástrico, por isso uma refeição "
                   "gorda tarde ainda está a ser digerida à hora de adormecer."),
    Link("late_meal_sleep_efficiency", "last_meal_hour", "sleep_efficiency_pct",
         DOWN, ("nutrition", "sleep"), min_n=20, min_effect=2,
         label_pt="hora do jantar e qualidade do sono",
         mechanism="Quanto mais tarde acaba a última refeição, mais a digestão "
                   "coincide com o início da noite e mais fragmentado fica o sono."),
    Link("big_day_awakenings", "total_cals_in", "sleep_awakenings", UP,
         ("nutrition", "sleep"), min_n=20, min_effect=0.5,
         label_pt="dias de excesso e noites cortadas",
         mechanism="Dias de ingestão muito acima do habitual tendem a fragmentar a "
                   "noite seguinte."),
    Link("fiber_next_day_bowel", "total_fiber_g", "bowel_movement", UP,
         ("nutrition", "self_report"), min_n=20, min_effect=0.1,
         label_pt="fibra e trânsito intestinal",
         mechanism="A fibra aumenta o volume e a velocidade do trânsito intestinal, "
                   "com um efeito que aparece tipicamente no dia seguinte."),

    # -- sleep -> next-day behaviour (same row: the night PRECEDES the day) ----
    Link("short_sleep_more_calories", "sleep_mins", "total_cals_in", DOWN,
         ("sleep", "nutrition"), min_n=20, min_effect=100,
         label_pt="dormir pouco e comer mais",
         mechanism="Dormir pouco desregula a grelina e a leptina e aumenta o apetite "
                   "por comida densa em energia no dia seguinte."),
    Link("short_sleep_less_movement", "sleep_mins", "steps", DOWN,
         ("sleep", "activity"), min_n=20, min_effect=800,
         label_pt="dormir pouco e mexer-se menos",
         mechanism="Uma noite curta reduz a atividade espontânea do dia seguinte "
                   "muito antes de se sentir como cansaço."),
    Link("poor_recovery_less_training", "hrv_ms", "workout_mins", UP,
         ("sleep", "activity"), min_n=20, min_effect=8,
         label_pt="recuperação e treino",
         mechanism="A variabilidade cardíaca da noite anterior acompanha a "
                   "disponibilidade para treinar nesse dia."),
    Link("short_sleep_sugar", "sleep_mins", "total_sugar_g", DOWN,
         ("sleep", "nutrition"), min_n=20, min_effect=8,
         label_pt="dormir pouco e procurar açúcar",
         mechanism="A privação de sono aumenta especificamente a procura de "
                   "hidratos rápidos, mais do que a fome em geral."),

    # -- training -> intake and recovery ---------------------------------------
    Link("training_day_protein", "workout_mins", "total_protein_g", UP,
         ("activity", "nutrition"), min_n=20, min_effect=10,
         label_pt="treino e proteína",
         mechanism="Os dias de treino são precisamente aqueles em que a proteína "
                   "mais conta para a recuperação e manutenção de músculo."),
    Link("training_deep_sleep", "workout_mins", "sleep_deep_mins", UP,
         ("activity", "sleep"), min_n=20, min_effect=6,
         label_pt="treino e sono profundo",
         mechanism="O exercício aumenta a pressão de sono e, com ela, a fatia de "
                   "sono profundo da noite seguinte."),
    Link("training_resting_hr", "workout_mins", "resting_hr_bpm", UP,
         ("activity", "sleep"), min_n=20, min_effect=1.5,
         label_pt="carga de treino e recuperação",
         mechanism="Uma sessão dura eleva a frequência cardíaca de repouso da noite "
                   "seguinte enquanto o corpo ainda está a recuperar."),

    # -- energy balance -> body (the loop the whole system exists to close) ----
    Link("deficit_weight_change", "energy_balance_kcal", "weight_kg", UP,
         ("nutrition", "activity", "body"), min_n=25, min_effect=0.15,
         label_pt="balanço energético e peso",
         mechanism="O balanço energético de um dia aparece na balança da manhã "
                   "seguinte, sobretudo através da água e do conteúdo intestinal."),
    Link("protein_lean_mass", "total_protein_g", "lean_mass_kg", UP,
         ("nutrition", "body"), min_n=25, min_effect=0.1,
         label_pt="proteína e massa magra",
         mechanism="A proteína é o que decide se o peso perdido vem da gordura ou "
                   "do músculo."),
    Link("sodium_morning_weight", "total_sodium_mg", "weight_kg", UP,
         ("nutrition", "body"), min_n=25, min_effect=0.2,
         label_pt="sal e peso da manhã seguinte",
         mechanism="O sódio retém água, por isso um dia salgado pesa na balança da "
                   "manhã seguinte sem que nada de gordura tenha mudado."),
)


def _offset(cause_causal: str, effect_causal: str) -> Optional[int]:
    """How many rows forward the effect sits, given when each thing is measured.

    Within one row the real chronology is: the night that ended this morning, then
    the weigh-in, then the day's food and movement. So:

      * input  -> outcome   the food on row N meets the night recorded on N+1  -> 1
      * outcome -> input    the night on row N precedes that same day's food   -> 0
      * same role           contemporaneous; association only                  -> 0

    Returns None for a pairing with no defined direction, so an ill-formed
    hypothesis is skipped rather than silently measured backwards.
    """
    cause_in = cause_causal in CAUSAL_INPUT
    effect_in = effect_causal in CAUSAL_INPUT
    cause_out = cause_causal in CAUSAL_OUTCOME
    effect_out = effect_causal in CAUSAL_OUTCOME
    if not (cause_in or cause_out) or not (effect_in or effect_out):
        return None
    if cause_in and effect_out:
        return 1
    return 0


def _truthy(value: Any) -> Optional[float]:
    """Booleans-in-a-spreadsheet as 1.0/0.0. `bowel_movement` is the only such
    column, and it is blank-means-no — but only on a day the user logged anything at
    all, which the caller decides."""
    text = str(value).strip().upper()
    if text in ("TRUE", "1", "SIM", "YES"):
        return 1.0
    if text in ("FALSE", "0", "NAO", "NÃO", "NO"):
        return 0.0
    return None


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        out = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        flag = _truthy(value)
        return flag
    return out if math.isfinite(out) else None


# -- meal-derived features ------------------------------------------------------

def daily_features(meals_by_day: Dict[str, Sequence[Dict[str, Any]]]
                   ) -> Dict[str, Dict[str, float]]:
    """day -> the meal-derived features for that day.

    Takes meals already bucketed by waking day (the caller owns the 05:00 cutoff,
    which lives in main.py and must not be re-implemented here). A day with no meals
    produces no entry at all rather than zeros: "ate nothing after 21:00" and "logged
    nothing that day" are different facts, and conflating them would manufacture
    correlations out of the days the user simply forgot to photograph.
    """
    out: Dict[str, Dict[str, float]] = {}
    for day, meals in meals_by_day.items():
        rows = [m for m in meals if isinstance(m, dict)]
        if not rows:
            continue
        feats: Dict[str, float] = {"meal_count": float(len(rows))}
        hours: List[float] = []
        late_cals = late_fat = 0.0
        largest = 0.0
        breakfast_protein: Optional[float] = None
        for meal in rows:
            stamp = str(meal.get("datetime") or "")
            hour: Optional[float] = None
            if len(stamp) >= 16 and stamp[10] in (" ", "T"):
                try:
                    hour = int(stamp[11:13]) + int(stamp[14:16]) / 60
                except ValueError:
                    hour = None
            calories = _num(meal.get("calories")) or 0.0
            fat = _num(meal.get("fat_g")) or 0.0
            protein = _num(meal.get("protein_g")) or 0.0
            largest = max(largest, calories)
            if hour is not None:
                # A 00:30 dessert belongs to the previous evening, not to a 0.5h
                # breakfast — the caller already filed it under the right waking
                # day, so shift it past midnight to keep "late" ordered correctly.
                adjusted = hour + 24 if hour < 5 else hour
                hours.append(adjusted)
                if adjusted >= 21:
                    late_cals += calories
                    late_fat += fat
                if adjusted <= 11 and (breakfast_protein is None
                                       or adjusted < feats.get("_bf_hour", 99)):
                    breakfast_protein = protein
                    feats["_bf_hour"] = adjusted
        if hours:
            feats["last_meal_hour"] = round(max(hours), 2)
        feats["calories_after_21h"] = round(late_cals, 1)
        feats["fat_g_after_21h"] = round(late_fat, 1)
        feats["largest_meal_kcal"] = round(largest, 1)
        if breakfast_protein is not None:
            feats["protein_at_breakfast_g"] = round(breakfast_protein, 1)
        feats.pop("_bf_hour", None)
        out[day] = feats
    return out


# -- the statistics -------------------------------------------------------------

def _tertile_contrast(pairs: Sequence[Tuple[float, float]]
                      ) -> Optional[Tuple[float, List[float], List[float]]]:
    """Split on the cause's tertiles and contrast the effect's means.

    Returns (high_mean - low_mean, high_group, low_group). Interpretable in the
    effect's own units, which is what a card needs — and far more robust to the
    outliers this data is full of than a correlation coefficient would be.
    """
    if len(pairs) < 6:
        return None
    ordered = sorted(pairs, key=lambda p: p[0])
    cut = max(2, len(ordered) // 3)
    low = [effect for _, effect in ordered[:cut]]
    high = [effect for _, effect in ordered[-cut:]]
    if not low or not high:
        return None
    return mean(high) - mean(low), high, low


def _permutation_p(high: Sequence[float], low: Sequence[float], observed: float,
                   rng: random.Random, rounds: int = PERMUTATIONS) -> float:
    """Two-sided p-value by relabelling.

    Exact and assumption-free: it asks how often a difference this large appears
    when the labels are shuffled, which is the honest question, and needs no
    distribution these metrics would not honour anyway.
    """
    pool = list(high) + list(low)
    size = len(high)
    target = abs(observed)
    hits = 0
    for _ in range(rounds):
        rng.shuffle(pool)
        if abs(mean(pool[:size]) - mean(pool[size:])) >= target:
            hits += 1
    # +1 on both sides: a p-value of exactly 0 is never justified by a finite
    # number of permutations.
    return (hits + 1) / (rounds + 1)


def _benjamini_hochberg(pvalues: Sequence[float], alpha: float = ALPHA
                        ) -> List[bool]:
    """Which hypotheses survive at an FDR of `alpha`.

    Testing two dozen hypotheses at p<0.05 buys roughly one false positive per run
    by construction — and this runs every day, so without a correction the coach
    would invent a chain most weeks. BH controls the expected *proportion* of false
    findings among those reported, which is exactly the guarantee wanted here.
    """
    if not pvalues:
        return []
    indexed = sorted(enumerate(pvalues), key=lambda pair: pair[1])
    total = len(pvalues)
    keep = [False] * total
    largest_k = -1
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= alpha * rank / total:
            largest_k = rank
    for rank, (index, _) in enumerate(indexed, start=1):
        if rank <= largest_k:
            keep[index] = True
    return keep


def _stable(pairs: Sequence[Tuple[float, float]], observed: float) -> bool:
    """Does the effect point the same way in both halves of the window?

    A link driven entirely by one strange fortnight is not a property of this
    person, and this is the cheapest possible guard against it.
    """
    half = len(pairs) // 2
    if half < 4:
        return True   # too short to split; the n and FDR gates carry it alone
    signs = []
    for chunk in (pairs[:half], pairs[half:]):
        contrast = _tertile_contrast(chunk)
        if contrast is None:
            return True
        signs.append(contrast[0])
    return (signs[0] >= 0) == (signs[1] >= 0) and (signs[0] >= 0) == (observed >= 0)


# -- the engine -----------------------------------------------------------------

def _value(day_row: Dict[str, Any], features: Dict[str, float],
           metric: str) -> Optional[float]:
    if metric in FEATURES:
        value = features.get(metric)
        return float(value) if value is not None else None
    if metric == "bowel_movement":
        # Blank means "not logged" for the purpose of a correlation. Treating it as
        # a false would invent constipation out of days the user just didn't note.
        return _truthy(day_row.get(metric))
    return _num(day_row.get(metric))


def causal_of(metric: str, columns: Dict[str, str]) -> Optional[str]:
    """The causal window of a metric, whether it is a column or a derived feature."""
    if metric in FEATURES:
        return FEATURES[metric].causal
    return columns.get(metric)


def evaluate(days: Sequence[Dict[str, Any]], *,
             features_by_day: Optional[Dict[str, Dict[str, float]]] = None,
             columns: Optional[Dict[str, str]] = None,
             blocks: Sequence[str] = (),
             links: Sequence[Link] = LINKS,
             seed: int = SEED,
             alpha: float = ALPHA) -> List[Dict[str, Any]]:
    """Every link the data supports, strongest first.

    `days` must be ascending by date, one dict per `daily_summary` row.
    `columns` maps a column name to its causal window (from the registry — passed in
    rather than imported so this module stays flattened-image friendly).
    `blocks` is the capability gate: a link is not even evaluated unless the user
    measures every block it spans, so a nutrition-only user gets an empty list
    without a single special case.

    Returns findings in the same shape as the rest of the coach, with `domain`
    fixed to "link" so the feed can budget them separately.
    """
    have = set(blocks)
    columns = columns or {}
    features_by_day = features_by_day or {}
    rng = random.Random(seed)

    by_date = {str(row.get("date") or ""): row for row in days}
    ordered_dates = sorted(d for d in by_date if d)

    candidates: List[Dict[str, Any]] = []
    for link in links:
        if not have.issuperset(link.blocks):
            continue
        cause_causal = causal_of(link.cause, columns)
        effect_causal = causal_of(link.effect, columns)
        if not cause_causal or not effect_causal:
            continue
        offset = _offset(cause_causal, effect_causal)
        if offset is None:
            continue

        pairs: List[Tuple[float, float]] = []
        for index, date in enumerate(ordered_dates):
            target_index = index + offset
            if target_index >= len(ordered_dates):
                continue
            cause_row = by_date[date]
            effect_row = by_date[ordered_dates[target_index]]
            cause = _value(cause_row, features_by_day.get(date, {}), link.cause)
            effect = _value(effect_row,
                            features_by_day.get(ordered_dates[target_index], {}),
                            link.effect)
            if cause is None or effect is None:
                continue
            pairs.append((cause, effect))

        if len(pairs) < link.min_n:
            continue
        contrast = _tertile_contrast(pairs)
        if contrast is None:
            continue
        delta, high, low = contrast
        if abs(delta) < link.min_effect:
            continue
        # The declared direction is a GATE, not an annotation. A hypothesis whose
        # mechanism says late eating costs deep sleep has nothing to say about data
        # showing the reverse — and handing the model a mechanism that contradicts
        # the number it must quote is how an incoherent card gets written. Each
        # hypothesis is therefore one-sided; the two-sided p-value kept below is
        # conservative for that, which is the safe direction to err in.
        if (delta > 0) != (link.expect == UP):
            continue
        if not _stable(pairs, delta):
            continue

        p = _permutation_p(high, low, delta, rng)
        candidates.append({"link": link, "delta": delta, "p": p, "n": len(pairs),
                           "offset": offset, "high": high, "low": low,
                           "cause_causal": cause_causal,
                           "effect_causal": effect_causal})

    survivors = _benjamini_hochberg([c["p"] for c in candidates], alpha)
    out: List[Dict[str, Any]] = []
    for candidate, kept in zip(candidates, survivors):
        if not kept:
            continue
        link: Link = candidate["link"]
        delta = candidate["delta"]
        # Severity orders the feed only: how confident, and how much of the effect's
        # own scale it moves.
        magnitude = abs(delta) / (abs(mean(candidate["low"])) or 1.0)
        severity = max(0.0, min(1.0, 0.5 * min(magnitude * 3, 1.0)
                                + 0.5 * (1 - candidate["p"] / alpha)))
        out.append({
            "id": f"link:{link.id}",
            "domain": "link",
            "kind": "link",
            "group": link.id,
            "severity": round(severity, 2),
            "headline": _headline(link, delta, candidate),
            "foods": [],
            "metrics": [link.cause, link.effect],
            "evidence": {
                "cause": link.cause,
                "effect": link.effect,
                "effect_delta": round(delta, 2),
                "n_days": candidate["n"],
                "p_value": round(candidate["p"], 4),
                "lag_days": candidate["offset"],
                # Always the declared one: a link is only surfaced when the data
                # moved the way its mechanism says it should.
                "expected_direction": link.expect,
                "mechanism": link.mechanism,
                "label_pt": link.label_pt,
                # Named explicitly so the card copy can never be written as causal:
                # this is an association in one person's observational data.
                "claim": "association",
            },
        })
    out.sort(key=lambda f: -f["severity"])
    return out


def _headline(link: Link, delta: float, candidate: Dict[str, Any]) -> str:
    """The plain factual sentence the model rewrites. Carries the contrast, the n and
    the lag, so a critic can check the written card against the arithmetic."""
    when = {0: "on the same day", 1: "on the following day"}[candidate["offset"]]
    direction = "higher" if delta > 0 else "lower"
    return (f"{link.label_pt}: when {link.cause} is in its top third, {link.effect} "
            f"is {abs(round(delta, 2))} {direction} {when} "
            f"(n={candidate['n']}, p={candidate['p']:.3f}) — association, not proof")
