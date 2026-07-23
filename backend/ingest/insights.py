"""The deterministic core of the weekly-insights coach (Stages A + B).

The model must never do the arithmetic. Everything here is pure code — it takes the
raw meal rows for a window plus the already-resolved targets (the same dict `/today`
serves, kinetics and all) and produces a **Diagnosis**: a structured, falsifiable set
of facts about the last N days. The local generator (`automation/insights/`) hands
this straight to the strong model, which only ever turns finished facts into words.

Why this lives in the backend, next to the nutrition science it depends on:

  * ONE source of truth. The RDA table, the daily-vs-rolling kinetics and the recomp
    formulas already live in `main.py`; re-deriving them in the local job would let the
    two drift on exactly the thing that must be correct. Instead the caller passes the
    resolved targets in, and this module reasons over them.
  * It is a pure function of its inputs, so the whole "deep, careful analysis" the
    feature promises is unit-tested with the rest of the backend and can be eyeballed
    via `/insights/diagnose` before a single model call is spent.

The three ideas that keep the analysis honest, all mechanical here:

  1. **Horizon-aware.** A rolling nutrient (B12, iron — body-banked) is judged on the
     window average; a daily one (magnesium, vitamin C — excreted) also on how many
     days actually cleared the floor. The horizon is read off the target, never
     guessed.
  2. **Coverage-gated.** A gap in extraction must not read as a gap in the diet. Each
     nutrient carries a `coverage` — how much of the window we actually have data for —
     and below the policy floor it reads `unknown`, never `deficit`.
  3. **Policy-judged.** A raw deficit/excess becomes a *genuine issue* only after the
     `nutrient_policy` gauntlet (goal weight, excess posture, food-source strength), so
     the model is never handed a false alarm (a cholesterol spike, a "low" vitamin D
     that really comes from sun).
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

# -- tunable thresholds --------------------------------------------------------
# A reach nutrient below this fraction of its floor (averaged over the window) is a
# deficit; at/above it is adequate. 0.8 matches the app's amber "close" band.
DEFICIT_RATIO = 0.80
# A limit nutrient above its ceiling is "over"; between NEAR_RATIO and 1.0 it is
# "near" (worth watching, not yet a problem).
LIMIT_NEAR_RATIO = 0.85
# A reach nutrient whose window average climbs past this fraction of its toxicity
# ceiling (UL) is flagged as approaching it — rare from food, but correct to catch.
UL_APPROACH_RATIO = 0.90
# How much a window-over-window move must be, relative, to count as a real trend
# rather than noise.
TREND_RATIO = 0.10
# An item is "well characterised" (its micros are known, present-or-genuinely-zero)
# when it carries at least this many nutrient keys. A grounded item has ~30; a sparse,
# un-audited estimate has ~6-9. This is what separates "we know it's ~0" from "we
# never extracted it" — the heart of the coverage guard.
GROUNDED_KEYS_PER_ITEM = 12

# Macros that are reported in the `adherence` block and so are not repeated as ranked
# nutrient issues (protein and fibre ARE repeated — they are also genuine issues).
_PURE_MACROS = ("calories", "carbs_g", "fat_g")

_ROLLING = "rolling"


# -- policy --------------------------------------------------------------------
def resolve_policy(policy: Dict[str, Any], key: str) -> Dict[str, Any]:
    """A nutrient's effective policy: the per-nutrient overrides layered on the
    defaults, so a nutrient absent from the file still gets sane rules."""
    defaults = policy.get("defaults", {})
    override = (policy.get("nutrients", {}) or {}).get(key, {})
    return {**defaults, **override}


# -- small pure helpers --------------------------------------------------------
def _num(value: Any) -> float:
    """A finite non-negative float, or 0.0 — intake is never negative."""
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and out not in (float("inf"), float("-inf")) and out > 0 else 0.0


def _parse_items(raw: Any) -> List[Dict[str, Any]]:
    """The `items` cell as a list of ingredient dicts (JSON string or already a list)."""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _norm_name(name: Any) -> str:
    return " ".join(str(name or "").lower().split())


def _item_keys(item: Dict[str, Any]) -> Dict[str, float]:
    n = item.get("nutrients")
    return n if isinstance(n, dict) else {}


def _key_density(items: Sequence[Dict[str, Any]]) -> float:
    """Average populated nutrient keys per item — the item-level grounding signal."""
    counted = [len(_item_keys(i)) for i in items if isinstance(i, dict)]
    return (sum(counted) / len(counted)) if counted else 0.0


def _is_real_meal(row: Dict[str, Any]) -> bool:
    """A row that actually contributed food (has parseable items and positive calories).
    Mirrors the ingest roll-up's stub/zero-row skip so every lens agrees."""
    if _num(row.get("calories")) <= 0:
        return False
    return bool(_parse_items(row.get("items")))


def _round_amount(key: str, value: float) -> float:
    return round(value, 2 if key.endswith("_g") else 1)


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


# -- coverage & attribution ----------------------------------------------------
def _coverage(window_meals: Sequence[Dict[str, Any]], key: str) -> float:
    """How much of the window we actually have data for this nutrient (0..1).

    A meal "knows" a nutrient when the key is explicitly present (>0) OR the meal is
    well grounded — a grounded meal's micros are all known, and a genuine zero (an
    apple has no B12) must count as knowledge, not as a missing reading. A sparse,
    un-audited meal that lacks the key counts as unknown. Denominator is the real
    meals in the window."""
    real = [r for r in window_meals if _is_real_meal(r)]
    if not real:
        return 0.0
    known = 0
    for row in real:
        items = _parse_items(row.get("items"))
        present = any(_num(_item_keys(i).get(key)) > 0 for i in items)
        grounded = _key_density(items) >= GROUNDED_KEYS_PER_ITEM
        if present or grounded:
            known += 1
    return round(known / len(real), 2)


def _attribution(window_meals: Sequence[Dict[str, Any]], key: str,
                 top: int = 3) -> List[Dict[str, Any]]:
    """The foods that contributed this nutrient across the window, biggest first.

    For a deficit these are the sources already working (eat more of them); for an
    excess they are what to cut. Aggregated by food name so 'ovo' eaten five times
    reads as one line. Pure summation over the stored per-ingredient nutrients."""
    sums: Dict[str, float] = {}
    total = 0.0
    for row in window_meals:
        if not _is_real_meal(row):
            continue
        for item in _parse_items(row.get("items")):
            amount = _num(_item_keys(item).get(key))
            if amount <= 0:
                continue
            name = _norm_name(item.get("name"))
            if not name:
                continue
            sums[name] = sums.get(name, 0.0) + amount
            total += amount
    if total <= 0:
        return []
    ranked = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [{"food": name, "amount": _round_amount(key, amt),
             "pct": round(100 * amt / total)} for name, amt in ranked]


# -- window statistics ---------------------------------------------------------
def _daily_values(days: Sequence[Dict[str, Any]], key: str) -> List[float]:
    """The per-day intake of `key` across the window (a missing day is not here at all;
    a day that logged food but not this nutrient contributes 0)."""
    return [_num((d.get("consumed") or {}).get(key)) for d in days]


def _window_mean(days: Sequence[Dict[str, Any]], key: str) -> float:
    return _mean(_daily_values(days, key))


def _trend(mean_now: float, mean_prev: float, kind: str) -> Optional[str]:
    """improving / declining / steady, read in the direction that helps: for a floor
    (reach) more is better; for a ceiling (limit) less is better. None when there is no
    prior window to compare against."""
    if mean_prev <= 0 and mean_now <= 0:
        return None
    base = mean_prev if mean_prev > 0 else mean_now
    if base <= 0:
        return None
    rel = (mean_now - mean_prev) / base
    if abs(rel) < TREND_RATIO:
        return "steady"
    up = rel > 0
    if kind == "limit":
        return "improving" if not up else "declining"
    return "improving" if up else "declining"


# -- the per-nutrient verdict --------------------------------------------------
def _judge_nutrient(key: str, mean: float, coverage: float,
                    target: Dict[str, Any], pol: Dict[str, Any]
                    ) -> Tuple[str, Any, Optional[float]]:
    """(status, genuine_issue, pct) for one nutrient, after the policy gauntlet.

    status ∈ deficit | adequate | over | over_benign | near | approaching_ul | unknown
    genuine_issue ∈ True | False | "weak"   (weak = worth a note, not an alarm)
    pct = mean / the relevant target (floor for reach, ceiling for limit), or None.
    """
    kind = target.get("kind", "reach")
    floor = target.get("floor")
    ceiling = target.get("ceiling")

    if coverage < float(pol.get("coverage_floor", 0.55)):
        return "unknown", False, None

    if kind == "limit":
        if not ceiling or ceiling <= 0:
            return "adequate", False, None
        pct = mean / ceiling
        posture = pol.get("excess_posture", "none")
        if pct > 1.0:
            if posture in ("flag", "hard_flag"):
                return "over", True, pct
            return "over_benign", False, pct
        if pct >= LIMIT_NEAR_RATIO:
            return "near", False, pct
        return "adequate", False, pct

    # reach (a floor to hit) — may also carry a toxicity ceiling (a UL from kinetics).
    if ceiling and ceiling > 0 and mean >= UL_APPROACH_RATIO * ceiling:
        posture = pol.get("excess_posture", "none")
        return "approaching_ul", (posture in ("flag", "hard_flag")), (
            mean / ceiling)
    if not floor or floor <= 0:
        return "adequate", False, None
    pct = mean / floor
    if pct < DEFICIT_RATIO:
        weak = pol.get("deficit_from_food") == "weak"
        return "deficit", ("weak" if weak else True), pct
    return "adequate", False, pct


def _severity(status: str, pct: Optional[float]) -> float:
    """How far off target, 0..1, for ranking. A deep deficit or a big overshoot scores
    high; an adequate nutrient scores 0."""
    if pct is None:
        return 0.0
    if status == "deficit":
        return max(0.0, min(1.0, 1.0 - pct))            # further below floor = worse
    if status in ("over", "approaching_ul"):
        return max(0.0, min(1.0, pct - 1.0))            # further above ceiling = worse
    return 0.0


# -- the Diagnosis -------------------------------------------------------------
def build_diagnosis(*, ref_day: str, window_days: int,
                    days: Sequence[Dict[str, Any]],
                    prev_days: Sequence[Dict[str, Any]],
                    window_meals: Sequence[Dict[str, Any]],
                    targets: Dict[str, Dict[str, Any]],
                    basis: Dict[str, Any],
                    policy: Dict[str, Any]) -> Dict[str, Any]:
    """The full deterministic Diagnosis for a window (see the module docstring).

    `days` / `prev_days` are the per-day intake windows ({date, consumed}) already
    computed by the backend's `_history_window`, oldest first; `window_meals` are the
    raw meal rows in the window (for attribution and coverage); `targets` is the
    resolved+kinetics target dict `/today` serves; `basis` its inputs. Nothing here
    reads a sheet or calls a model."""
    real_meals = [r for r in window_meals if _is_real_meal(r)]
    days_logged = len(days)
    weight = _num(basis.get("weight_kg")) or None

    adherence = _adherence(days, targets, weight)
    nutrients_out: List[Dict[str, Any]] = []
    for key, target in targets.items():
        if key in _PURE_MACROS:
            continue                         # reported in `adherence`, not as an issue
        mean = _window_mean(days, key)
        coverage = _coverage(window_meals, key)
        pol = resolve_policy(policy, key)
        status, genuine, pct = _judge_nutrient(key, mean, coverage, target, pol)
        prev_mean = _window_mean(prev_days, key) if prev_days else 0.0
        trend = _trend(mean, prev_mean, target.get("kind", "reach")) if prev_days else None
        entry: Dict[str, Any] = {
            "key": key,
            "horizon": target.get("horizon", "daily"),
            "unit": target.get("unit", ""),
            "mean": _round_amount(key, mean),
            "target": target.get("floor") if target.get("kind") != "limit"
                      else target.get("ceiling"),
            "kind": target.get("kind", "reach"),
            "pct": round(pct, 2) if pct is not None else None,
            "coverage": coverage,
            "status": status,
            "genuine_issue": genuine,
            "trend": trend,
            "goal_weight": float(pol.get("goal_weight", 0.4)),
        }
        if status in ("deficit", "over", "over_benign", "near", "approaching_ul"):
            entry["attribution"] = _attribution(window_meals, key)
        if pol.get("note") and (genuine or status in ("over_benign", "approaching_ul")):
            entry["note"] = pol["note"]
        nutrients_out.append(entry)

    ranked = _rank_issues(nutrients_out)
    wins = _wins(adherence, nutrients_out)
    coverage_note = _coverage_note(nutrients_out)

    return {
        "window": {
            "start": days[0]["date"] if days else None,
            "end": days[-1]["date"] if days else None,
            "ref_day": ref_day,
            "days_logged": days_logged,
            "meals_logged": len(real_meals),
        },
        "adherence": adherence,
        "nutrients": nutrients_out,
        "ranked_issues": ranked,
        "wins": wins,
        "correlations": [],                  # pre-registered set added in a later pass
        "coverage_note": coverage_note,
        "basis": {
            "weight_kg": basis.get("weight_kg"),
            "calorie_target_kcal": basis.get("calorie_target_kcal"),
            "protein_g_per_kg": basis.get("protein_g_per_kg"),
            "goal": basis.get("goal"),
        },
    }


def _adherence(days: Sequence[Dict[str, Any]], targets: Dict[str, Dict[str, Any]],
               weight: Optional[float]) -> Dict[str, Any]:
    """The headline macro stats: mean intake and how consistently the window hit each
    target. Protein and calories carry the numbers the recomp goal turns on."""
    out: Dict[str, Any] = {}
    n = len(days)

    def block(key: str) -> Optional[Dict[str, Any]]:
        target = targets.get(key)
        if not target:
            return None
        vals = _daily_values(days, key)
        mean = _mean(vals)
        kind = target.get("kind", "reach")
        floor, ceiling = target.get("floor"), target.get("ceiling")
        entry: Dict[str, Any] = {"mean": _round_amount(key, mean), "kind": kind,
                                 "unit": target.get("unit", ""), "days": n}
        if kind == "window" and floor and ceiling:
            entry["floor"], entry["ceiling"] = floor, ceiling
            entry["days_on_target"] = sum(1 for v in vals if floor <= v <= ceiling)
            entry["pct"] = round(mean / ((floor + ceiling) / 2), 2)
        elif kind == "limit" and ceiling:
            entry["ceiling"] = ceiling
            entry["days_under"] = sum(1 for v in vals if v <= ceiling)
            entry["pct"] = round(mean / ceiling, 2) if ceiling else None
        elif floor:
            entry["target"] = floor
            entry["days_hit"] = sum(1 for v in vals if v >= floor)          # green/"met"
            entry["days_close"] = sum(1 for v in vals if v >= floor * DEFICIT_RATIO)
            entry["pct"] = round(mean / floor, 2) if floor else None
        return entry

    for key in ("calories", "protein_g", "carbs_g", "fat_g", "fiber_g"):
        b = block(key)
        if b is not None:
            out[key] = b
    if weight and out.get("protein_g"):
        out["protein_g"]["per_kg"] = round(out["protein_g"]["mean"] / weight, 2)
    return out


def _rank_issues(nutrients: Sequence[Dict[str, Any]]) -> List[str]:
    """The prioritised shortlist: genuine issues only, scored goal_weight × severity so
    the week leads with the one change that matters most (protein for recomp before a
    trace vitamin). `weak` issues are notes, not headline problems, so they rank below
    any real one."""
    scored: List[Tuple[float, str]] = []
    for n in nutrients:
        genuine = n.get("genuine_issue")
        if not genuine:
            continue
        sev = _severity(n["status"], n.get("pct"))
        weight = n.get("goal_weight", 0.4)
        # goal_weight is the PRIMARY key (protein is the recomp hero — a moderate
        # protein gap must lead over a deep gap in a trace nutrient); severity is the
        # tiebreaker within a weight band.
        score = weight * (0.5 + 0.5 * sev)
        if genuine == "weak":
            score *= 0.35                                # a note ranks below any alarm
        scored.append((score, n["key"]))
    scored.sort(reverse=True)
    return [key for _, key in scored]


def _wins(adherence: Dict[str, Any], nutrients: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What is going well — reinforcement is behaviour science, not filler. Rewarding a
    kept habit is why it survives, so every week names at least the best thing."""
    wins: List[Dict[str, Any]] = []
    prot = adherence.get("protein_g")
    if prot and prot.get("target") and prot.get("days_hit", 0) >= max(1, prot["days"] - 1):
        wins.append({"kind": "consistency", "key": "protein_g",
                     "detail": f"protein hit {prot['days_hit']}/{prot['days']} days"})
    for n in nutrients:
        if n.get("trend") == "improving" and n["status"] in ("adequate", "deficit"):
            wins.append({"kind": "trend", "key": n["key"],
                         "detail": f"{n['key']} improving vs last week"})
    for n in nutrients:
        if n.get("status") == "adequate" and n.get("goal_weight", 0) >= 0.55 \
                and n.get("coverage", 0) >= 0.6:
            wins.append({"kind": "adequate", "key": n["key"],
                         "detail": f"{n['key']} comfortably on target"})
    # de-dupe by key, keep order (consistency > trend > adequate), cap at 3.
    seen, out = set(), []
    for w in wins:
        if w["key"] in seen:
            continue
        seen.add(w["key"])
        out.append(w)
    return out[:3]


def _coverage_note(nutrients: Sequence[Dict[str, Any]]) -> str:
    unknown = [n["key"] for n in nutrients if n["status"] == "unknown"]
    if not unknown:
        return "Boa cobertura de dados esta semana."
    shown = ", ".join(unknown[:4])
    more = f" (+{len(unknown) - 4})" if len(unknown) > 4 else ""
    return (f"Dados insuficientes para avaliar: {shown}{more}. "
            f"Não são tratados como carência — só como desconhecido.")


# -- food vocabulary (Stage E input) -------------------------------------------
_CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("vegetable", ("brócolo", "brocolo", "espinafre", "alface", "tomate", "cenoura",
                   "courgette", "cebola", "pimento", "couve", "salada", "legume",
                   "grelos", "feijão verde", "spinach", "broccoli", "lettuce",
                   "carrot", "salad", "vegetable", "kale", "pepper", "onion")),
    ("fruit", ("maçã", "maca", "banana", "laranja", "morango", "uva", "pera", "kiwi",
               "manga", "ananás", "melão", "fruta", "apple", "orange", "berry",
               "strawberry", "grape", "fruit", "pineapple", "melon")),
    ("legume", ("feijão", "feijao", "grão", "grao", "lentilha", "ervilha", "bean",
                "lentil", "chickpea", "pea")),
    ("protein_animal", ("frango", "peru", "vaca", "porco", "bife", "carne", "peixe",
                        "salmão", "salmao", "atum", "bacalhau", "sardinha", "ovo",
                        "ovos", "chicken", "beef", "pork", "fish", "salmon", "tuna",
                        "egg", "turkey", "steak", "meat")),
    ("dairy", ("leite", "iogurte", "queijo", "requeijão", "milk", "yogurt", "yoghurt",
               "cheese", "skyr", "kefir")),
    ("grain_starch", ("arroz", "massa", "esparguete", "pão", "pao", "batata", "aveia",
                      "cereais", "tostas", "bolacha", "rice", "pasta", "bread", "potato",
                      "oats", "cereal", "quinoa", "couscous")),
    ("nut_seed", ("amêndoa", "amendoa", "noz", "nozes", "amendoim", "caju", "semente",
                  "almond", "walnut", "peanut", "cashew", "seed", "chia")),
    ("fat_oil", ("azeite", "óleo", "oleo", "manteiga", "abacate", "olive oil", "butter",
                 "avocado", "oil")),
    ("sweet_snack", ("chocolate", "bolo", "bolacha", "gelado", "doce", "açúcar",
                     "acucar", "snack", "cake", "cookie", "ice cream", "candy", "sweet")),
    ("beverage", ("café", "cafe", "chá", "cha", "sumo", "refrigerante", "cerveja",
                  "vinho", "coffee", "tea", "juice", "soda", "beer", "wine")),
]


def categorize_food(name: str) -> str:
    """A coarse food category from the name (heuristic, pt-PT + en keywords). Powers
    'you eat nothing from category X' and 'swap within the same category'. Deliberately
    simple — a miss lands in 'other', never crashes."""
    text = _norm_name(name)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(k in text for k in keywords):
            return category
    return "other"


def _meal_slot(datetime_str: str) -> str:
    """breakfast / morning_snack / lunch / afternoon_snack / dinner from the meal's
    local hour. Distinguishes morning vs afternoon snacks for the timing profile."""
    hour = 0
    try:
        hour = int(str(datetime_str)[11:13])
    except (ValueError, IndexError):
        pass
    if 5 <= hour < 11:
        return "breakfast"
    if 11 <= hour < 15:
        return "lunch"
    if 18 <= hour < 23:
        return "dinner"
    if 15 <= hour < 18:
        return "afternoon_snack"
    return "morning_snack"  # 0-5 or 23+


def build_food_profile(window_meals: Sequence[Dict[str, Any]],
                       nutrient_keys: Sequence[str]) -> List[Dict[str, Any]]:
    """The user's food vocabulary, mined from their logged meals: for each distinct
    food, how often and when it's eaten, a typical portion, and its per-gram nutrient
    density (so the next-meal engine can compute how much of it closes a gap). Pure
    function of the meals — rebuilt each run, so it can never go stale."""
    agg: Dict[str, Dict[str, Any]] = {}
    for row in window_meals:
        if not _is_real_meal(row):
            continue
        slot = _meal_slot(row.get("datetime", ""))
        when = str(row.get("datetime") or "")
        for item in _parse_items(row.get("items")):
            name = _norm_name(item.get("name"))
            portion = _num(item.get("portion_g"))
            if not name or portion <= 0:
                continue
            rec = agg.setdefault(name, {
                "food": name, "category": categorize_food(name), "times_eaten": 0,
                "slots": {}, "portions": [], "last_eaten": "",
                "_nsum": {}, "_gsum": 0.0, "cal_per_g": 0.0, "_calsum": 0.0,
            })
            rec["times_eaten"] += 1
            rec["slots"][slot] = rec["slots"].get(slot, 0) + 1
            rec["portions"].append(portion)
            rec["last_eaten"] = max(rec["last_eaten"], when)
            rec["_gsum"] += portion
            rec["_calsum"] += _num(item.get("calories"))
            for k in nutrient_keys:
                v = _num(_item_keys(item).get(k))
                if v > 0:
                    rec["_nsum"][k] = rec["_nsum"].get(k, 0.0) + v

    out: List[Dict[str, Any]] = []
    for rec in agg.values():
        gsum = rec.pop("_gsum") or 0.0
        nsum = rec.pop("_nsum")
        calsum = rec.pop("_calsum") or 0.0
        density = {k: round(v / gsum, 4) for k, v in nsum.items()} if gsum > 0 else {}
        rec["density_per_g"] = density
        rec["cal_per_g"] = round(calsum / gsum, 3) if gsum > 0 else 0.0
        rec["median_portion_g"] = round(statistics.median(rec.pop("portions")))
        rec["top_slot"] = max(rec["slots"], key=rec["slots"].get) if rec["slots"] else "snack"
        out.append(rec)
    out.sort(key=lambda r: r["times_eaten"], reverse=True)
    return out


# -- next-meal portion math (Stage E) ------------------------------------------
def portion_range(gap_amount: float, density_per_g: float, cal_per_g: float,
                  calorie_budget: float, *, min_serving_g: float = 40,
                  max_serving_g: float = 300) -> Optional[Tuple[int, int]]:
    """The gram range of a food that meaningfully closes `gap_amount` of a nutrient,
    bounded by a sane serving and by the calories left in the day. The model chooses
    WHICH palatable food; this fixes HOW MUCH, so the quantity on screen is always a
    real number, never a guess. Returns (low, high) grams, or None if the food can't
    help (no density) or there's no calorie room."""
    if density_per_g <= 0 or gap_amount <= 0:
        return None
    grams_to_close = gap_amount / density_per_g
    cal_cap = (calorie_budget / cal_per_g) if cal_per_g > 0 else max_serving_g
    upper = min(max_serving_g, cal_cap)
    if upper < min_serving_g:
        return None                          # not enough calorie room for a real serving
    target = max(min_serving_g, min(grams_to_close, upper))
    low = max(min_serving_g, round(target * 0.85 / 5) * 5)
    high = min(round(upper), round(max(target, low) * 1.15 / 5) * 5)
    return (int(low), int(max(high, low)))


def next_meal_context(*, consumed: Dict[str, float], targets: Dict[str, Dict[str, Any]],
                      focus_key: Optional[str], food_profile: Sequence[Dict[str, Any]],
                      slot: str) -> Dict[str, Any]:
    """What the next-meal model needs, all deterministic: how much of the day's budget
    is left, which nutrients are still short today, and — for each shortfall — the foods
    the user already eats that are densest in it, with the gram range that would close
    it. The model assembles palatable plates from these; the numbers are ours."""
    cal_t = targets.get("calories", {})
    cal_left = max(0.0, (cal_t.get("ceiling") or cal_t.get("floor") or 0) - _num(consumed.get("calories")))
    prot_t = targets.get("protein_g", {})
    prot_left = max(0.0, (prot_t.get("floor") or 0) - _num(consumed.get("protein_g")))

    shortfalls: List[str] = []
    for key, target in targets.items():
        if target.get("kind") == "limit" or key in _PURE_MACROS:
            continue
        floor = target.get("floor")
        if floor and _num(consumed.get(key)) < DEFICIT_RATIO * floor:
            shortfalls.append(key)
    # focus (this week's headline gap) leads, then today's other shortfalls.
    ordered = ([focus_key] if focus_key and focus_key in [t for t in targets] else []) + \
              [k for k in shortfalls if k != focus_key]

    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for key in ordered[:4]:
        floor = targets.get(key, {}).get("floor") or 0
        gap = max(0.0, floor - _num(consumed.get(key)))
        if gap <= 0:
            continue
        ranked = sorted(
            (f for f in food_profile if f.get("density_per_g", {}).get(key, 0) > 0),
            key=lambda f: f["density_per_g"][key], reverse=True)[:6]
        rows = []
        for f in ranked:
            rng = portion_range(gap, f["density_per_g"][key], f.get("cal_per_g", 0), cal_left)
            if rng:
                rows.append({"food": f["food"], "category": f["category"],
                             "grams_low": rng[0], "grams_high": rng[1],
                             "times_eaten": f["times_eaten"]})
        if rows:
            candidates[key] = rows

    return {
        "slot": slot,
        "calories_left": round(cal_left),
        "protein_left_g": round(prot_left),
        "focus_key": focus_key,
        "shortfalls_today": ordered[:4],
        "candidates": candidates,
    }


# -- Meal timing profile (for dynamic next-slot detection) ---------------------

def _slot_time_hour(datetime_str: str) -> Optional[int]:
    """Extract the hour from a datetime string for timing analysis."""
    try:
        return int(str(datetime_str)[11:13])
    except (ValueError, IndexError):
        return None


def build_meal_timing_profile(window_meals: Sequence[Dict[str, Any]],
                               window_days: int) -> Dict[str, Any]:
    """Build a profile of the user's typical meal timing over the window.

    Returns a dict like:
    {
      "breakfast": {"pct": 0.90, "typical_time": "08:15", "times_eaten": 25},
      "lunch": {"pct": 0.95, "typical_time": "13:00", "times_eaten": 27},
      "dinner": {"pct": 0.90, "typical_time": "20:00", "times_eaten": 25},
      "morning_snack": {"pct": 0.25, "typical_time": "10:30", "times_eaten": 7},
      "afternoon_snack": {"pct": 0.40, "typical_time": "16:30", "times_eaten": 11}
    }

    Used by the AI to decide which meal slot is next for the user.
    """
    slot_data: Dict[str, List[int]] = {
        "breakfast": [], "morning_snack": [], "lunch": [],
        "afternoon_snack": [], "dinner": [],
    }

    days_with_slot: Dict[str, set] = {
        s: set() for s in slot_data
    }

    for row in window_meals:
        if not _is_real_meal(row):
            continue
        slot = _meal_slot(row.get("datetime", ""))
        hour = _slot_time_hour(row.get("datetime", ""))
        day = str(row.get("datetime", ""))[:10]
        if slot not in slot_data:
            continue
        if hour is not None:
            slot_data[slot].append(hour)
        if day:
            days_with_slot[slot].add(day)

    window_days = max(window_days, 1)
    profile: Dict[str, Any] = {}
    for slot, hours in slot_data.items():
        times_eaten = len(hours)
        pct = round(times_eaten / max(len(days_with_slot[slot]), 1) if days_with_slot[slot] else 0, 2)
        # Clamp pct: if they ate it on 90% of days where they had that slot...
        # Actually, use unique days for pct
        unique_days = len(days_with_slot[slot])
        pct = round(unique_days / window_days, 2) if window_days > 0 else 0.0
        typical = ""
        if hours:
            avg_hour = statistics.mean(hours)
            mins = int((avg_hour - int(avg_hour)) * 60)
            typical = f"{int(avg_hour):02d}:{mins:02d}" if avg_hour >= 5 else ""

        profile[slot] = {
            "pct": min(pct, 1.0),
            "typical_time": typical,
            "times_eaten": times_eaten,
        }
    return profile


def build_today_meals_summary(today_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a summary of today's logged meals for the AI context.

    Returns list like:
    [{"time": "08:15", "slot": "breakfast", "foods": "aveia, banana, leite",
      "calories": 420, "protein_g": 18}]
    """
    summary: List[Dict[str, Any]] = []
    for row in today_rows:
        if not _is_real_meal(row):
            continue
        dt = str(row.get("datetime", ""))
        time_str = dt[11:16] if len(dt) >= 16 else ""
        slot = _meal_slot(dt)
        # Collect food names from items.
        item_names = []
        for item in _parse_items(row.get("items")):
            name = _norm_name(item.get("name"))
            if name:
                item_names.append(name)

        summary.append({
            "time": time_str,
            "slot": slot,
            "foods": ", ".join(item_names[:5]) if item_names else "",
            "calories": round(_num(row.get("calories"))),
            "protein_g": round(_num(row.get("protein_g")), 1),
        })
    summary.sort(key=lambda m: m["time"])
    return summary


def next_meal_context_v2(*, consumed: Dict[str, float],
                          targets: Dict[str, Dict[str, Any]],
                          focus_key: Optional[str],
                          food_profile: Sequence[Dict[str, Any]],
                          today_rows: Sequence[Dict[str, Any]],
                          window_meals: Sequence[Dict[str, Any]],
                          window_days: int,
                          current_time: str) -> Dict[str, Any]:
    """Enhanced next-meal context that includes the user's timing patterns and
    today's logged meals so the AI can determine the next slot dynamically.

    Returns the full context dict for the narrator's `assemble_next_meal()` call.
    """
    # Same shortfall/candidate logic as next_meal_context().
    cal_t = targets.get("calories", {})
    cal_left = max(0.0, (cal_t.get("ceiling") or cal_t.get("floor") or 0) - _num(consumed.get("calories")))
    prot_t = targets.get("protein_g", {})
    prot_left = max(0.0, (prot_t.get("floor") or 0) - _num(consumed.get("protein_g")))

    shortfalls: List[str] = []
    for key, target in targets.items():
        if target.get("kind") == "limit" or key in _PURE_MACROS:
            continue
        floor = target.get("floor")
        if floor and _num(consumed.get(key)) < DEFICIT_RATIO * floor:
            shortfalls.append(key)

    ordered = ([focus_key] if focus_key and focus_key in [t for t in targets] else []) + \
              [k for k in shortfalls if k != focus_key]

    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for key in ordered[:4]:
        floor = targets.get(key, {}).get("floor") or 0
        gap = max(0.0, floor - _num(consumed.get(key)))
        if gap <= 0:
            continue
        ranked = sorted(
            (f for f in food_profile if f.get("density_per_g", {}).get(key, 0) > 0),
            key=lambda f: f["density_per_g"][key], reverse=True)[:6]
        rows = []
        for f in ranked:
            rng = portion_range(gap, f["density_per_g"][key], f.get("cal_per_g", 0), cal_left)
            if rng:
                rows.append({"food": f["food"], "category": f["category"],
                             "grams_low": rng[0], "grams_high": rng[1],
                             "times_eaten": f["times_eaten"]})
        if rows:
            candidates[key] = rows

    # Build timing profile and today's meals summary.
    meal_pattern = build_meal_timing_profile(window_meals, window_days)
    today_meals = build_today_meals_summary(today_rows)

    return {
        "current_time": current_time,
        "today_meals": today_meals,
        "meal_pattern": meal_pattern,
        "calories_left": round(cal_left),
        "protein_left_g": round(prot_left),
        "focus_key": focus_key,
        "shortfalls_today": ordered[:4],
        "candidates": candidates,
    }
