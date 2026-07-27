"""Deterministic findings for everything that isn't food.

`food_patterns.build_findings` decides what is worth one sentence about the user's
eating. This is the same job for sleep, overnight recovery, training, body
composition and digestion — and it deliberately produces the **same finding shape**,
so the feed, the cooldowns, the archive and the card assembly all work on the new
domains without learning anything new.

## Why this is not a model's job

The temptation is to hand a model 90 rows of 80 columns and ask what it notices. It
will always notice something, and some of it will be invented — a fluent, confident
sentence about a pattern that isn't in the data. Worse, a wrong finding is *acted
on*. So every claim the coach can make about a number originates here, in code that
can be unit-tested, and the model's job is strictly to say it well.

## What makes a finding specialist rather than generic

Three properties, all deterministic and all cheap:

1. **Judged against this person, not a population.** "You slept 6h20" is generic;
   "your worst four nights in a row this month, an hour below your own average" is
   specialist. Every rule here reads the window's own mean and spread — the same
   idea the `baselines` tab is built on — and the few absolute thresholds that
   remain are ones with real physiological meaning (an efficiency below ~85%),
   never a magazine number.
2. **The right horizon.** One short night is Tuesday; four in a row is a pattern.
   Every rule declares how many days it needs and how many of them must be bad,
   so a single off day can never produce a card.
3. **Silence when the data is thin.** Below `MIN_DAYS` a rule does not fire at all.
   "Not enough data yet" is the honest answer and the app already knows how to show
   nothing.

## Severity

A blunt 0..1 score used only for ordering — how far past the line, clipped. The feed
takes the top findings across all domains and then applies its own per-domain budget,
so severity decides *which* sleep finding is the sleep finding, not whether sleep
gets to speak at all.

Pure stdlib. No sheet, no network: it takes rows and returns findings, which is what
makes every rule below testable in one line.
"""
from __future__ import annotations

import json
import math
import os
from statistics import mean, pstdev
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Domains, matching schema.capabilities.BLOCK_DOMAINS. Duplicated as bare strings
# rather than imported because this module is flattened into /app next to main.py
# in the container image while the registry lives in a `schema` package — the same
# reason food_patterns doesn't import from src/.
SLEEP, ACTIVITY, BODY, DIGESTION = "sleep", "activity", "body", "digestion"

# Nothing fires below this many days with data for the metric in question. Two
# weeks is the shortest window in which "four bad nights" means anything.
MIN_DAYS = 10

# A z-score this far from the personal mean is where "unusual for me" starts. 1.0 SD
# is deliberately gentle: these rules also require the deviation to PERSIST, and
# stacking two strict gates produces a coach that never speaks.
Z_NOTABLE = 1.0

POLICY_FILE = os.path.join(os.path.dirname(__file__), "domain_policy.json")


def _num(value: Any) -> Optional[float]:
    """A sheet cell as a float, or None. Booleans are rejected on purpose: a TRUE in
    `bowel_movement` is a flag, and letting it silently become 1.0 in a numeric rule
    is the kind of bug that produces confident nonsense."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _series(days: Sequence[Dict[str, Any]], metric: str) -> List[float]:
    """Every present value for a metric, oldest first. Blanks are skipped, not
    zero-filled — a night the tracker wasn't worn is missing data, and averaging a
    zero into it would invent a catastrophe."""
    return [v for v in (_num(d.get(metric)) for d in days) if v is not None]


def _tail(days: Sequence[Dict[str, Any]], metric: str, n: int) -> List[float]:
    return _series(days[-n:] if n else days, metric)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _finding(domain: str, kind: str, severity: float, *, headline: str,
             evidence: Dict[str, Any], metrics: Sequence[str] = (),
             group: Optional[str] = None) -> Dict[str, Any]:
    """One observation, in the exact shape `food_patterns._finding` produces.

    `foods` is present and empty: the feed's swap validation reads it for every
    finding, and a sleep finding legitimately has no food attached. Keeping the key
    means no caller needs to know which domain a finding came from.
    """
    return {
        "id": f"{domain}:{kind}" + (f":{group}" if group else ""),
        "domain": domain,
        "kind": kind,
        "group": group,
        "severity": round(_clip01(severity), 2),
        "headline": headline,
        "evidence": evidence,
        "foods": [],
        "metrics": list(metrics),
    }


# =============================================================================
# The declarative rules
#
# Most findings are the same shape: a metric that has been persistently worse than
# this person's own normal (or past a threshold that means something physiologically)
# for enough of the recent window to be a pattern rather than a day. Writing twenty
# bespoke functions for that would be twenty places for the same bug, so the shape is
# a table and only the genuinely bespoke rules below are code.
#
# Each rule declares:
#   metric      the daily_summary column it reads
#   direction   "low" or "high" — which side is the problem
#   window      how many recent days it looks at
#   min_bad     how many of those must be past the line before it is a pattern
#   floor/ceil  an ABSOLUTE line, where one is physiologically meaningful
#   vs_baseline compare against the person's own mean instead (in SDs)
#   headline    a plain factual sentence; the model rewrites it warmly
# =============================================================================

_RULES: Tuple[Dict[str, Any], ...] = (
    # -- sleep ----------------------------------------------------------------
    {
        "domain": SLEEP, "kind": "short_sleep", "metric": "sleep_mins",
        "direction": "low", "window": 7, "min_bad": 3, "floor": 420,
        "label": "sono",
        "headline": "sleep under 7h on {bad} of the last {window} nights "
                    "(average {avg_h}h)",
    },
    {
        "domain": SLEEP, "kind": "sleep_below_own_average", "metric": "sleep_mins",
        "direction": "low", "window": 7, "min_bad": 4, "vs_baseline": Z_NOTABLE,
        "label": "sono",
        "headline": "sleep has been {delta} min below the usual on {bad} of the "
                    "last {window} nights",
    },
    {
        "domain": SLEEP, "kind": "low_efficiency", "metric": "sleep_efficiency_pct",
        "direction": "low", "window": 7, "min_bad": 3, "floor": 85,
        "label": "eficiência do sono",
        "headline": "sleep efficiency under 85% on {bad} of the last {window} "
                    "nights (average {avg}%) — time in bed that isn't sleep",
    },
    {
        "domain": SLEEP, "kind": "long_latency", "metric": "sleep_latency_mins",
        "direction": "high", "window": 7, "min_bad": 3, "ceiling": 30,
        "label": "adormecer",
        "headline": "took over 30 min to fall asleep on {bad} of the last "
                    "{window} nights (average {avg} min)",
    },
    {
        "domain": SLEEP, "kind": "low_deep_sleep", "metric": "sleep_deep_mins",
        "direction": "low", "window": 7, "min_bad": 4, "vs_baseline": Z_NOTABLE,
        "label": "sono profundo",
        "headline": "deep sleep {delta} min below the usual on {bad} of the last "
                    "{window} nights",
    },
    {
        "domain": SLEEP, "kind": "fragmented", "metric": "sleep_awakenings",
        "direction": "high", "window": 7, "min_bad": 4, "vs_baseline": Z_NOTABLE,
        "label": "noites cortadas",
        "headline": "more awakenings than usual on {bad} of the last {window} nights",
    },
    # -- overnight recovery (folded into the sleep domain — one subject to a reader)
    {
        "domain": SLEEP, "kind": "resting_hr_elevated", "metric": "resting_hr_bpm",
        "direction": "high", "window": 5, "min_bad": 3, "vs_baseline": Z_NOTABLE,
        "label": "frequência cardíaca em repouso",
        "headline": "resting heart rate {delta} bpm above the usual on {bad} of "
                    "the last {window} nights",
    },
    {
        "domain": SLEEP, "kind": "hrv_suppressed", "metric": "hrv_ms",
        "direction": "low", "window": 5, "min_bad": 3, "vs_baseline": Z_NOTABLE,
        "label": "variabilidade cardíaca",
        "headline": "overnight HRV below the usual on {bad} of the last {window} "
                    "nights",
    },
    {
        "domain": SLEEP, "kind": "skin_temp_elevated", "metric": "skin_temp_dev",
        "direction": "high", "window": 4, "min_bad": 3, "ceiling": 0.6,
        "label": "temperatura da pele",
        "headline": "skin temperature {avg}C above the personal baseline on {bad} "
                    "of the last {window} nights",
    },
    # -- activity -------------------------------------------------------------
    {
        "domain": ACTIVITY, "kind": "steps_below_usual", "metric": "steps",
        "direction": "low", "window": 7, "min_bad": 4, "vs_baseline": Z_NOTABLE,
        "label": "passos",
        "headline": "steps below the usual on {bad} of the last {window} days "
                    "(average {avg})",
    },
    {
        "domain": ACTIVITY, "kind": "very_sedentary", "metric": "sedentary_mins",
        "direction": "high", "window": 7, "min_bad": 4, "ceiling": 690,
        "label": "tempo sentado",
        "headline": "over 11.5h sedentary on {bad} of the last {window} days",
    },
)


def _rule_findings(days: Sequence[Dict[str, Any]],
                   rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate one declarative rule. None when it doesn't fire.

    The baseline, when a rule uses one, is computed over the WHOLE window rather
    than the recent slice — otherwise a bad fortnight quietly becomes the new normal
    and the rule goes quiet exactly when it matters most.
    """
    metric = rule["metric"]
    whole = _series(days, metric)
    if len(whole) < MIN_DAYS:
        return None

    window = int(rule["window"])
    recent = _tail(days, metric, window)
    if len(recent) < max(3, window // 2):
        return None

    high = rule["direction"] == "high"
    line: Optional[float] = rule.get("ceiling") if high else rule.get("floor")
    spread = pstdev(whole) if len(whole) >= 3 else 0.0
    avg_all = mean(whole)

    if line is None:
        # Personal-baseline rule: the line is the person's own mean, moved by the
        # configured number of SDs. With no spread at all there is no such thing as
        # unusual, so the rule stays silent rather than firing on noise.
        if not spread:
            return None
        line = avg_all + (rule["vs_baseline"] * spread if high
                          else -rule["vs_baseline"] * spread)

    bad = [v for v in recent if (v > line if high else v < line)]
    if len(bad) < int(rule["min_bad"]):
        return None

    avg_bad = mean(bad)
    # How far past the line, as a fraction of the line itself (or of the spread for a
    # baseline rule), plus how much of the window was bad. Ordering only.
    scale = spread if rule.get("vs_baseline") else abs(line) or 1.0
    distance = abs(avg_bad - line) / (abs(scale) or 1.0)
    severity = _clip01(0.35 * min(distance, 2.0) + 0.45 * (len(bad) / window))

    avg = round(mean(recent), 1)
    headline = rule["headline"].format(
        bad=len(bad), window=window, avg=avg, avg_h=round(avg / 60, 1),
        delta=abs(round(avg_bad - avg_all)))
    return _finding(
        rule["domain"], rule["kind"], severity, headline=headline,
        metrics=[metric],
        evidence={
            "metric": metric,
            "label_pt": rule.get("label", metric),
            "days_bad": len(bad),
            "window_days": window,
            "recent_average": avg,
            "personal_average": round(avg_all, 1),
            "line": round(line, 1),
        })


# =============================================================================
# The bespoke rules — patterns that are not "a metric crossed a line"
# =============================================================================

def _bedtime_irregularity(days: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """How much the time of falling asleep moves around.

    Deliberately its own rule rather than a threshold on a number, because the
    evidence here is unusually strong and unusually counter-intuitive: *when* you
    sleep, held steady, does more for how you feel than the total does. Someone
    sleeping a consistent 7h is generally better off than someone averaging 7h30
    across a 3-hour spread. Nothing else in the schema captures it, and no total
    ever will.

    Clock times need care: 23:50 and 00:10 are twenty minutes apart, not 23 hours.
    Each time is mapped onto a circle and averaged as a vector, so the spread is
    computed the way a clock actually works.
    """
    stamps = [str(d.get("sleep_start") or "").strip() for d in days]
    minutes: List[float] = []
    for stamp in stamps:
        parts = stamp.split(":")
        if len(parts) < 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            minutes.append(hour * 60 + minute)
    if len(minutes) < MIN_DAYS:
        return None

    # Circular spread: average the unit vectors, and the shorter the resultant the
    # more scattered the times. R -> [0, 1]; 1 is a perfectly fixed bedtime.
    angles = [2 * math.pi * m / 1440 for m in minutes]
    cos_mean = mean(math.cos(a) for a in angles)
    sin_mean = mean(math.sin(a) for a in angles)
    resultant = math.hypot(cos_mean, sin_mean)
    if resultant <= 0:
        return None
    # Circular SD in minutes, via the standard sqrt(-2 ln R).
    spread_min = math.sqrt(max(0.0, -2 * math.log(resultant))) * 1440 / (2 * math.pi)

    policy = _policy().get("bedtime_spread_mins", {})
    line = float(policy.get("line", 60))
    if spread_min < line:
        return None

    mean_angle = math.atan2(sin_mean, cos_mean) % (2 * math.pi)
    centre = int(round(1440 * mean_angle / (2 * math.pi))) % 1440
    severity = _clip01((spread_min - line) / 60)
    return _finding(
        SLEEP, "irregular_bedtime", severity,
        headline=(f"bedtime moves by about {round(spread_min)} min either side of "
                  f"{centre // 60:02d}:{centre % 60:02d} — an irregular schedule, "
                  f"independent of how long the nights are"),
        metrics=["sleep_start"],
        evidence={"metric": "sleep_start", "label_pt": "hora de deitar",
                  "spread_mins": round(spread_min),
                  "typical_time": f"{centre // 60:02d}:{centre % 60:02d}",
                  "nights": len(minutes), "line": line})


def _training_findings(days: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Training as the user's goal actually cares about it.

    Two things matter and neither is a step count. **Strength work** is what decides
    whether a calorie deficit costs muscle, so its absence is the single most
    important activity finding for anyone recomposing. And a **long gap** since the
    last session is worth naming while it is still a gap rather than a lapsed habit.
    """
    out: List[Dict[str, Any]] = []
    if len(days) < MIN_DAYS:
        return out

    policy = _policy().get("training", {})
    strength_words = tuple(policy.get("strength_types",
                                      ["strength", "weight", "força", "musculação"]))
    weeks = max(1.0, len(days) / 7.0)

    sessions = 0
    strength_days = 0
    gap = 0
    seen_any = False
    for day in days:
        count = _num(day.get("workout_count")) or 0
        types = str(day.get("workout_types") or "").lower()
        if count > 0:
            sessions += int(count)
            seen_any = True
            gap = 0
        else:
            gap += 1
        if any(word in types for word in strength_words):
            strength_days += 1

    if not seen_any:
        return out   # nothing logged at all is a data gap, not a training finding

    per_week = strength_days / weeks
    min_per_week = float(policy.get("strength_sessions_per_week", 2))
    if per_week < min_per_week:
        out.append(_finding(
            ACTIVITY, "little_strength_work",
            _clip01((min_per_week - per_week) / min_per_week),
            headline=(f"{round(per_week, 1)} strength sessions a week over the last "
                      f"{len(days)} days, against a reference of {min_per_week:g} — "
                      f"strength work is what protects muscle in a deficit"),
            metrics=["workout_types", "workout_count"],
            evidence={"metric": "workout_types", "label_pt": "treino de força",
                      "sessions_per_week": round(per_week, 1),
                      "reference_per_week": min_per_week,
                      "window_days": len(days)}))

    max_gap = float(policy.get("max_gap_days", 5))
    if gap >= max_gap:
        out.append(_finding(
            ACTIVITY, "training_gap", _clip01((gap - max_gap) / 7 + 0.3),
            headline=f"no workout logged for {gap} days",
            metrics=["workout_count"],
            evidence={"metric": "workout_count", "label_pt": "treino",
                      "days_since_last": gap, "line": max_gap}))
    return out


def _body_findings(days: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Is the recomposition actually working?

    The north star, and the one number pair that answers it: lean mass and fat mass
    moving in opposite directions. Weight alone cannot tell you — losing 2 kg is a
    success or a failure depending entirely on which tissue went.

    Compared as the mean of the first half of the window against the mean of the
    second, never first-reading against last: bioimpedance is hydration-sensitive
    enough that any two single readings can say whatever you like.
    """
    out: List[Dict[str, Any]] = []
    policy = _policy().get("body", {})
    min_days = int(policy.get("min_days", 14))

    lean = _series(days, "lean_mass_kg")
    fat_pct = _series(days, "body_fat_pct")
    if len(lean) < min_days or len(fat_pct) < min_days:
        return out

    def _shift(values: List[float]) -> float:
        half = len(values) // 2
        return mean(values[half:]) - mean(values[:half])

    lean_shift, fat_shift = _shift(lean), _shift(fat_pct)
    lean_line = float(policy.get("lean_loss_kg", 0.4))
    fat_line = float(policy.get("fat_pct_move", 0.4))

    if lean_shift <= -lean_line:
        # The warning that matters during a cut, and the one a scale weight hides.
        out.append(_finding(
            BODY, "losing_lean_mass", _clip01(abs(lean_shift) / (2 * lean_line)),
            headline=(f"lean mass down {abs(round(lean_shift, 2))} kg across the "
                      f"window while body fat moved {round(fat_shift, 1)}pp — the "
                      f"weight coming off is not only fat"),
            metrics=["lean_mass_kg", "body_fat_pct"],
            evidence={"metric": "lean_mass_kg", "label_pt": "massa magra",
                      "lean_change_kg": round(lean_shift, 2),
                      "body_fat_change_pp": round(fat_shift, 1),
                      "days": len(lean)}))
    elif fat_shift <= -fat_line and lean_shift >= -0.1:
        # Worth saying out loud: this is the goal, and a coach that only ever
        # reports problems is one the user stops opening.
        out.append(_finding(
            BODY, "recomposition_working", 0.5,
            headline=(f"body fat down {abs(round(fat_shift, 1))}pp while lean mass "
                      f"held ({round(lean_shift, 2):+} kg) — textbook recomposition"),
            metrics=["body_fat_pct", "lean_mass_kg"],
            evidence={"metric": "body_fat_pct", "label_pt": "composição corporal",
                      "body_fat_change_pp": round(fat_shift, 1),
                      "lean_change_kg": round(lean_shift, 2),
                      "days": len(fat_pct), "good_news": True}))
    return out


def _digestion_findings(days: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bowel movements against fibre.

    `bowel_movement` is a blank-means-no flag, so this only reads days that have any
    self-report at all — otherwise a stretch when the user simply stopped logging
    would read as constipation. It is paired with fibre because that is the lever
    the rest of the app can actually move.
    """
    out: List[Dict[str, Any]] = []
    policy = _policy().get("digestion", {})
    window = int(policy.get("window_days", 14))
    recent = list(days[-window:])
    if len(recent) < int(policy.get("min_days", 10)):
        return out

    logged = [d for d in recent if str(d.get("bowel_movement") or "").strip() != ""]
    if len(logged) < int(policy.get("min_logged_days", 7)):
        return out

    def _truthy(value: Any) -> bool:
        return str(value).strip().upper() in ("TRUE", "1", "SIM", "YES")

    hit_days = sum(1 for d in logged if _truthy(d.get("bowel_movement")))
    rate = hit_days / len(logged)
    line = float(policy.get("min_rate", 0.6))
    if rate >= line:
        return out

    fiber = _series(recent, "total_fiber_g")
    avg_fiber = round(mean(fiber), 1) if fiber else None
    out.append(_finding(
        DIGESTION, "infrequent_bowel_movements", _clip01((line - rate) / line),
        headline=(f"a bowel movement on {hit_days} of the {len(logged)} days logged"
                  + (f", with fibre averaging {avg_fiber} g" if avg_fiber else "")),
        metrics=["bowel_movement", "total_fiber_g"],
        evidence={"metric": "bowel_movement", "label_pt": "trânsito intestinal",
                  "days_with_movement": hit_days, "days_logged": len(logged),
                  "average_fiber_g": avg_fiber}))
    return out


# =============================================================================
# Policy + entry point
# =============================================================================

_policy_cache: Dict[str, Any] = {}


def _policy() -> Dict[str, Any]:
    """The tunable thresholds, from `domain_policy.json`. Mirrors how
    `nutrient_policy.json` holds the "genuine issue vs non-problem" rules for
    nutrients: the numbers that encode judgement live in a file a human can review
    and change, not scattered through the code. Never fatal — a missing or invalid
    file just means every rule takes its built-in default."""
    if _policy_cache:
        return _policy_cache
    try:
        with open(POLICY_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        _policy_cache.update(data if isinstance(data, dict) else {})
    except (OSError, ValueError):
        _policy_cache["_"] = True   # cache the failure; don't retry on every call
    return _policy_cache


_BESPOKE: Tuple[Tuple[str, Callable[..., Any]], ...] = (
    (SLEEP, _bedtime_irregularity),
    (ACTIVITY, _training_findings),
    (BODY, _body_findings),
    (DIGESTION, _digestion_findings),
)


def build_findings(days: Sequence[Dict[str, Any]], *,
                   domains: Sequence[str] = (SLEEP, ACTIVITY, BODY, DIGESTION),
                   limit_per_domain: int = 2) -> List[Dict[str, Any]]:
    """Every non-food finding the window supports, highest severity first.

    `domains` is the capability gate and the only place it needs to appear: a
    nutrition-only user passes an empty tuple and gets an empty list, with no rule
    having to know that friends exist.

    `days` must be ascending by date, one dict per row of `daily_summary`.
    """
    wanted = set(domains)
    if not wanted or not days:
        return []

    found: List[Dict[str, Any]] = []
    for rule in _RULES:
        if rule["domain"] not in wanted:
            continue
        hit = _rule_findings(days, rule)
        if hit:
            found.append(hit)
    for domain, builder in _BESPOKE:
        if domain not in wanted:
            continue
        result = builder(days)
        if isinstance(result, dict):
            found.append(result)
        elif result:
            found.extend(result)

    found.sort(key=lambda f: -f["severity"])

    # Cap per domain here as well as in the feed: one domain having a genuinely
    # awful week must not crowd every other domain out of the ranking before the
    # feed ever sees them.
    kept: List[Dict[str, Any]] = []
    per_domain: Dict[str, int] = {}
    for finding in found:
        domain = finding["domain"]
        if per_domain.get(domain, 0) >= limit_per_domain:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        kept.append(finding)
    return kept
