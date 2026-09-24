"""Strength-training sessions: from a transcribed screenshot to five daily columns.

The training app (Hevy) is the sensor; a screenshot of the finished session is the
capture. This module holds everything that happens *after* the model has read the
screen — normalisation, plausibility, the arithmetic, and the roll-up. The model
transcribes; nothing here asks it to compute. That is the house rule
(`_meal_totals` follows it for food) and it matters more here than anywhere, because
a wrong load silently rewrites a progression trend.

Pure stdlib and Flask-free on purpose, exactly like `links.py` and `calibration.py`:
every rule below is a unit test rather than a live screenshot.

## Why there is no total-tonnage column

The obvious roll-up is Σ(load × reps) across the session, and it is close to
useless. It rises when you add three sets of curls and falls when you swap
squats for lunges, so it cannot answer the only question worth asking — *are my
loads going up?* Two sessions with the same tonnage can be a great day and a bad
one.

`load_index` answers it instead, by normalising each exercise against **its own**
recent best before averaging. That is not a new idea here: the `baselines` tab
already argues that an absolute value is uninterpretable and only becomes a
sentence against a personal baseline. This is the same move, applied to lifts.

  per exercise:  best e1RM today ÷ best e1RM in the previous 28 days
  index       :  mean of those ratios × 100

So 100 means "exactly at my baseline", 107 means "7% above it", and the number is
comparable across sessions that share no exercises at all. It is blank — not zero —
until an exercise has history to compare against, because "no baseline" and "worse
than baseline" are different facts.

## The traps, all of them specific to this screen

Reading digits off a phone screen is where a model lies most convincingly (the
scale path learned this the hard way, and `_normalize_body` is its scar). The
training screen has four of its own:

* **Warm-up sets.** Hevy marks them, and counting them inflates set volume by
  20-30% and drags every e1RM down. They are excluded from `sets`, `hard_sets`
  and the index — but kept in `sets_json`, because the screenshot said they
  happened.
* **Previous-session values and PR badges.** The screen prints them next to the
  real numbers with the same labels. Same shape as the scale app's "since <date>"
  delta block, and the prompt has to reject them explicitly; here we can only
  catch what falls outside a band.
* **Units.** kg or lb depending on the user's setting. The unit is read off the
  screen and converted *here*, never by the model.
* **Bodyweight and assisted movements.** A pull-up has no external load and an
  assisted dip has a negative one, so `weight_kg = 0` is legitimate — unlike
  `portion_g = 0` on the food path, which is always an error. Without
  `load_type` a back session would roll up to an index of zero and read as a
  catastrophic strength loss.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LB_TO_KG = 0.45359237

# How a set was performed. Only `normal` and `failure` count as working sets;
# `warmup` is recorded but never scored, and `drop` is a continuation of the set
# before it rather than a fresh one.
SET_TYPES = ("normal", "warmup", "drop", "failure")
WORKING_SET_TYPES = frozenset({"normal", "failure", "drop"})
SCORED_SET_TYPES = frozenset({"normal", "failure"})

# Where the resistance comes from. This is what stops a pull-up reading as 0 kg.
LOAD_TYPES = ("external", "bodyweight", "assisted", "band")

# Plausibility bands for a single set, the direct analogue of registry.ocr_ranges()
# for the scale. A value outside its band is dropped rather than written: a misread
# load is worse than a missing one, because it becomes a baseline.
SET_RANGES: Dict[str, Tuple[float, float]] = {
    "weight_kg": (0.0, 500.0),
    "reps": (1, 100),
    "rir": (0, 10),
}
SESSION_RANGES: Dict[str, Tuple[float, float]] = {
    "duration_min": (1, 300),
}

# A set at or below this RIR is "hard" — the volume hypertrophy actually responds
# to. Two reps from failure is the standard threshold and matches the plan's own
# RIR-2 prescription.
HARD_SET_RIR = 2

# Days of history a baseline is computed over. Matches the `baselines` tab's window,
# deliberately: two different definitions of "recent" in one sheet is a trap.
BASELINE_DAYS = 28

# Epley's coefficient. e1RM = load x (1 + reps/30). Chosen over Brzycki because it
# stays sane at the high-rep end (Brzycki diverges past ~12 reps, and a 20-rep set
# of lateral raises would produce nonsense).
_EPLEY = 30.0

# Above this many reps the formula is extrapolating well past what it was fitted
# on, so the set is recorded but not used to estimate a 1RM.
MAX_REPS_FOR_E1RM = 15


def _num(value: Any) -> Optional[float]:
    """A finite real number, or None. Rejects bools (a True load is not 1 kg) and
    the NaN that a model occasionally emits as a literal."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _in_band(key: str, value: float) -> bool:
    low, high = SET_RANGES[key]
    return low <= value <= high


def normalize_sets(raw: Any, *, unit: str = "kg",
                   log: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Keep the sets that are plausibly real, converted to kg.

    The load-bearing guard on this path, and the counterpart of `_normalize_body`.
    A set missing `reps` is not a set; a set outside a band is a misread and is
    dropped with a log line rather than trusted.
    """
    if not isinstance(raw, list):
        return []
    factor = LB_TO_KG if str(unit or "kg").strip().lower() in ("lb", "lbs") else 1.0
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("exercise") or "").strip().lower()
        if not name:
            continue

        reps = _num(entry.get("reps"))
        if reps is None or not _in_band("reps", reps):
            if log is not None and reps is not None:
                log.warning("set %s: reps=%s outside %s — dropped as a misread",
                            name, reps, SET_RANGES["reps"])
            continue

        load_type = str(entry.get("load_type") or "external").strip().lower()
        if load_type not in LOAD_TYPES:
            load_type = "external"
        set_type = str(entry.get("set_type") or "normal").strip().lower()
        if set_type not in SET_TYPES:
            set_type = "normal"

        weight = _num(entry.get("weight_kg"))
        if weight is not None:
            weight = round(weight * factor, 2)
            if not _in_band("weight_kg", weight):
                if log is not None:
                    log.warning("set %s: weight=%s kg outside %s — dropped as a "
                                "misread", name, weight, SET_RANGES["weight_kg"])
                continue
        elif load_type == "external":
            # An external-load set with no readable load carries no information we
            # can score; keeping it would corrupt both counts and the index.
            continue

        rir = _num(entry.get("rir"))
        if rir is not None and not _in_band("rir", rir):
            rir = None  # an implausible RIR is unknown, not a reason to drop the set

        row: Dict[str, Any] = {
            "exercise": name,
            "set_type": set_type,
            "load_type": load_type,
            "weight_kg": weight,
            "reps": int(reps),
        }
        name_pt = str(entry.get("exercise_pt") or "").strip().lower()
        if name_pt and name_pt != name:
            row["exercise_pt"] = name_pt
        muscle = str(entry.get("muscle_group") or "").strip().lower()
        if muscle:
            row["muscle_group"] = muscle
        if rir is not None:
            row["rir"] = int(rir)
        out.append(row)
    return out


def working_sets(sets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Everything that wasn't a warm-up."""
    return [s for s in sets if s.get("set_type") in WORKING_SET_TYPES]


def hard_sets(sets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Working sets taken to within HARD_SET_RIR of failure.

    A set with no RIR recorded does NOT count. That is deliberate and it biases
    low: over-counting hard sets would tell the user they hit a volume target they
    did not hit, which is the one error that makes the number worth nothing.
    """
    return [s for s in working_sets(sets)
            if _num(s.get("rir")) is not None and s["rir"] <= HARD_SET_RIR]


def effective_load_kg(entry: Dict[str, Any],
                      bodyweight_kg: Optional[float]) -> Optional[float]:
    """What the muscle actually moved, in kg.

    Bodyweight work needs the user's own weight to mean anything: a pull-up is not
    a 0 kg lift and a +10 kg pull-up is not a 10 kg one. Without a weigh-in we
    return None — the set is recorded but stays out of the index, rather than
    entering it as a number we made up.
    """
    added = _num(entry.get("weight_kg")) or 0.0
    kind = entry.get("load_type")
    if kind == "external":
        return added if added > 0 else None
    if kind in ("bodyweight", "assisted"):
        if bodyweight_kg is None or bodyweight_kg <= 0:
            return None
        total = bodyweight_kg + added if kind == "bodyweight" else bodyweight_kg - added
        return round(total, 2) if total > 0 else None
    return None  # bands: resistance is unknown and not linear in any readable way


def e1rm(load_kg: float, reps: float) -> float:
    """Estimated one-rep max, Epley. See _EPLEY on why not Brzycki."""
    return load_kg * (1.0 + reps / _EPLEY)


def best_e1rm_by_exercise(sets: Sequence[Dict[str, Any]],
                          bodyweight_kg: Optional[float] = None,
                          ) -> Dict[str, float]:
    """The best estimated 1RM per exercise across these sets.

    Warm-ups and drop sets are excluded: a drop set is performed pre-fatigued, so
    its e1RM understates the exercise and would drag a good session below baseline.
    """
    best: Dict[str, float] = {}
    for entry in sets:
        if entry.get("set_type") not in SCORED_SET_TYPES:
            continue
        reps = _num(entry.get("reps"))
        if reps is None or reps > MAX_REPS_FOR_E1RM:
            continue
        load = effective_load_kg(entry, bodyweight_kg)
        if load is None:
            continue
        value = e1rm(load, reps)
        name = entry["exercise"]
        if value > best.get(name, 0.0):
            best[name] = round(value, 2)
    return best


def load_index(today: Dict[str, float],
               baseline: Dict[str, float]) -> Optional[float]:
    """How heavy today was against this person's own recent best, as a percentage.

    Only exercises present on BOTH sides count — an exercise done for the first
    time has nothing to be compared against, and scoring it as 100 would quietly
    pull the index toward the middle on every session that introduces a movement.
    Returns None when nothing overlaps, which is the honest answer for the first
    four weeks.
    """
    ratios = [today[name] / baseline[name]
              for name in today
              if baseline.get(name, 0.0) > 0]
    if not ratios:
        return None
    return round(100.0 * sum(ratios) / len(ratios), 1)


def baseline_e1rms(history: Iterable[Dict[str, Any]], *, upto_date: str,
                   days: int = BASELINE_DAYS) -> Dict[str, float]:
    """Best e1RM per exercise over the `days` before `upto_date`, excluding it.

    Excluding the day itself is what makes the published index out-of-sample for
    its own row — the same discipline `src/calibration.py` insists on for the
    energy-balance correction, and for the same reason: a baseline that includes
    today can only ever say today was average.
    """
    from datetime import date, timedelta
    try:
        end = date.fromisoformat(upto_date)
    except (TypeError, ValueError):
        return {}
    start = end - timedelta(days=days)
    best: Dict[str, float] = {}
    for session in history or ():
        day = str(session.get("date") or "")
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        if not (start <= when < end):
            continue
        for name, value in (session.get("e1rms") or {}).items():
            if value > best.get(name, 0.0):
                best[name] = value
    return best


def _format_load(entry: Dict[str, Any]) -> str:
    """One set's load, as it should read to a human: 60, PC, PC+5, PC-20."""
    added = _num(entry.get("weight_kg")) or 0.0
    kind = entry.get("load_type")
    if kind == "bodyweight":
        return "PC" if added == 0 else f"PC+{added:g}"
    if kind == "assisted":
        return "PC" if added == 0 else f"PC-{added:g}"
    if kind == "band":
        return "elástico"
    return f"{added:g}"


def _span(values: Sequence[str]) -> str:
    """"8" when every set matched, "8-10" when they ranged."""
    first, last = values[0], values[-1]
    return first if first == last else f"{first}-{last}"


def summary_line(title: str, sets: Sequence[Dict[str, Any]]) -> str:
    """The whole session in one readable line.

    Deliberately compact text rather than JSON. The measured argument in
    CONTEXT.md §2b is that JSON costs ~5x the tokens because it repeats every key
    on every row, and this string is read on every row of every CSV export and
    every coach prompt — including the rest days where it is empty. A model reads
    "supino 4x8@60" and knows the exercise, the sets, the reps and the load; it
    learns nothing more from four quoted key names around them.
    """
    scored = working_sets(sets)
    if not scored:
        return str(title or "").strip()

    order: List[str] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in scored:
        name = entry.get("exercise_pt") or entry["exercise"]
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(entry)

    parts: List[str] = []
    for name in order:
        entries = grouped[name]
        reps = _span([f"{e['reps']:g}" for e in entries])
        loads = _span([_format_load(e) for e in entries])
        parts.append(f"{name} {len(entries)}×{reps}@{loads}")

    hard = len(hard_sets(sets))
    tail = f"{len(scored)} séries"
    if hard:
        tail += f" ({hard} duras)"
    rirs = [s["rir"] for s in scored if _num(s.get("rir")) is not None]
    if rirs:
        tail += f" · RIR {sum(rirs) / len(rirs):.1f}".replace(".", ",")

    head = str(title or "").strip()
    body = " · ".join(parts + [tail])
    return f"{head} · {body}" if head else body


def daily_row(sessions: Sequence[Dict[str, Any]], *,
              bodyweight_kg: Optional[float] = None,
              history: Optional[Iterable[Dict[str, Any]]] = None,
              date: str = "") -> Dict[str, Any]:
    """The `daily_summary` training columns for one day.

    Several sessions on one day fold into one row rather than overwriting each
    other — the same rule `biometrics.daily_exercise` applies to the tracker's own
    sessions. The day is scored as a whole: the best e1RM for an exercise is the
    best across every session in it.
    """
    if not sessions:
        return {}

    all_sets: List[Dict[str, Any]] = []
    titles: List[str] = []
    lines: List[str] = []
    for session in sessions:
        sets = list(session.get("sets") or ())
        all_sets.extend(sets)
        title = str(session.get("title") or "").strip()
        if title:
            titles.append(title)
        lines.append(summary_line(title, sets))

    working = working_sets(all_sets)
    if not working:
        return {}

    mins = sum(_num(s.get("duration_min")) or 0.0 for s in sessions)

    row: Dict[str, Any] = {
        "lift_sets": len(working),
        "lift_hard_sets": len(hard_sets(all_sets)),
        "lift_session": " + ".join(titles),
        "lift_summary": " | ".join(line for line in lines if line),
    }
    if mins > 0:
        row["lift_mins"] = round(mins)

    today = best_e1rm_by_exercise(all_sets, bodyweight_kg)
    index = load_index(today, baseline_e1rms(history or (), upto_date=date))
    if index is not None:
        row["lift_load_index"] = index
    return row
