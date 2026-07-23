"""Unit tests for the deterministic insights core (ingest/insights.py).

Pure functions over meal rows + resolved targets + policy — no sheet, no model, no
credentials. This is the "deep, careful analysis" the coach promises; it must be
falsifiable, so every judgment (coverage gating, deficit vs weak-note, excess posture,
attribution, portion math) is pinned here.
"""
import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parent.parent / "ingest" / "insights.py"
_spec = importlib.util.spec_from_file_location("ingest_insights", _PATH)
insights = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(insights)


# -- fixtures ------------------------------------------------------------------
POLICY = {
    "defaults": {"goal_weight": 0.4, "excess_posture": "none",
                 "deficit_from_food": "strong", "coverage_floor": 0.55},
    "nutrients": {
        "protein_g": {"goal_weight": 1.0},
        "fiber_g": {"goal_weight": 0.75},
        "omega3_g": {"goal_weight": 0.7, "coverage_floor": 0.45},
        "saturated_fat_g": {"goal_weight": 0.7, "excess_posture": "flag"},
        "sodium_mg": {"goal_weight": 0.55, "excess_posture": "flag"},
        "vitamin_d_ug": {"goal_weight": 0.6, "deficit_from_food": "weak",
                         "coverage_floor": 0.4, "note": "sun is the real source"},
        "iron_mg": {"goal_weight": 0.6, "excess_posture": "flag"},
    },
}

TARGETS = {
    "calories": {"kind": "window", "floor": 1800, "ceiling": 2100, "unit": "kcal",
                 "horizon": "daily"},
    "protein_g": {"kind": "reach", "floor": 140, "unit": "g", "horizon": "daily"},
    "carbs_g": {"kind": "window", "floor": 180, "ceiling": 240, "unit": "g",
                "horizon": "daily"},
    "fat_g": {"kind": "window", "floor": 56, "ceiling": 70, "unit": "g",
              "horizon": "daily"},
    "fiber_g": {"kind": "reach", "floor": 30, "unit": "g", "horizon": "daily"},
    "omega3_g": {"kind": "reach", "floor": 1.6, "unit": "g", "horizon": "rolling"},
    "vitamin_d_ug": {"kind": "reach", "floor": 15, "unit": "ug", "horizon": "rolling"},
    "iron_mg": {"kind": "reach", "floor": 8, "ceiling": 45, "unit": "mg",
                "horizon": "rolling"},
    "saturated_fat_g": {"kind": "limit", "ceiling": 20, "unit": "g", "horizon": "daily"},
    "sodium_mg": {"kind": "limit", "ceiling": 2300, "unit": "mg", "horizon": "daily"},
}

BASIS = {"weight_kg": 70.0, "calorie_target_kcal": 1930, "protein_g_per_kg": 2.0,
         "goal": "recomp"}

# ~30 nutrient keys, enough to read as a grounded (well-characterised) item.
_GROUNDED = {f"k{i}_mg": 1.0 for i in range(30)}


def grounded(**extra):
    """A nutrient map that reads as fully characterised, with real overrides on top."""
    return {**_GROUNDED, **extra}


def meal(dt, name, portion, cals, protein, nutrients):
    return {
        "datetime": dt, "foods": name, "calories": cals, "protein_g": protein,
        "carbs_g": 0, "fat_g": 0,
        "items": [{"name": name, "portion_g": portion, "calories": cals,
                   "protein_g": protein, "carbs_g": 0, "fat_g": 0,
                   "nutrients": nutrients}],
    }


def day(date, **consumed):
    return {"date": date, "consumed": consumed}


# -- policy --------------------------------------------------------------------
def test_resolve_policy_merges_defaults():
    prot = insights.resolve_policy(POLICY, "protein_g")
    assert prot["goal_weight"] == 1.0            # override
    assert prot["excess_posture"] == "none"      # default
    unknown = insights.resolve_policy(POLICY, "manganese_mg")
    assert unknown["goal_weight"] == 0.4         # falls back to defaults entirely


# -- coverage guard ------------------------------------------------------------
def test_sparse_data_reads_as_unknown_not_deficit():
    """Omega-3 absent AND meals sparse (few keys) → we don't know, so never a deficit."""
    meals = [meal(f"2026-07-1{i} 13:00", "arroz", 200, 300, 5,
                  {"sodium_mg": 5.0}) for i in range(1, 8)]     # 1-key items = sparse
    days = [day(f"2026-07-1{i}", calories=300, protein_g=5) for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    o3 = next(n for n in diag["nutrients"] if n["key"] == "omega3_g")
    assert o3["status"] == "unknown"
    assert o3["genuine_issue"] is False
    assert "omega3_g" not in diag["ranked_issues"]


def test_grounded_zero_counts_as_known():
    """A grounded meal with genuinely no omega-3 (an apple has no B12) is DATA, not a
    gap — so omega-3 is judged (a deficit), not hidden as unknown."""
    meals = [meal(f"2026-07-1{i} 13:00", "salada", 200, 300, 5, grounded())
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=300, protein_g=5) for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    o3 = next(n for n in diag["nutrients"] if n["key"] == "omega3_g")
    assert o3["coverage"] >= 0.9
    assert o3["status"] == "deficit"
    assert o3["genuine_issue"] is True


# -- deficit strength ----------------------------------------------------------
def test_weak_source_deficit_is_a_note_not_an_alarm():
    """Vitamin D low from food is a note (sun is the real source), ranked below any
    real deficit."""
    meals = [meal(f"2026-07-1{i} 13:00", "salada", 200, 300, 200, grounded())
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=300, protein_g=200, vitamin_d_ug=1.0)
            for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    vd = next(n for n in diag["nutrients"] if n["key"] == "vitamin_d_ug")
    assert vd["status"] == "deficit"
    assert vd["genuine_issue"] == "weak"
    assert vd.get("note")                          # the sun caveat travels with it


def test_protein_deficit_outranks_a_trace_vitamin():
    meals = [meal(f"2026-07-1{i} 13:00", "salada", 200, 300, 40, grounded(iron_mg=0.1))
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=300, protein_g=40, iron_mg=0.1)
            for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    assert diag["ranked_issues"][0] == "protein_g"   # goal_weight 1.0 leads


# -- excess posture ------------------------------------------------------------
def test_sodium_over_ceiling_is_a_genuine_issue():
    meals = [meal(f"2026-07-1{i} 13:00", "sopa", 300, 300, 10, grounded(sodium_mg=3500))
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=300, protein_g=10, sodium_mg=3500)
            for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    na = next(n for n in diag["nutrients"] if n["key"] == "sodium_mg")
    assert na["status"] == "over"
    assert na["genuine_issue"] is True


def test_over_a_ceiling_with_posture_none_is_benign():
    """The cholesterol lesson generalised: over a ceiling but posture=none → over_benign,
    never a ranked alarm."""
    policy = {**POLICY, "nutrients": {**POLICY["nutrients"],
                                      "sodium_mg": {"excess_posture": "none"}}}
    meals = [meal(f"2026-07-1{i} 13:00", "sopa", 300, 300, 10, grounded(sodium_mg=3500))
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=300, protein_g=10, sodium_mg=3500)
            for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=policy)
    na = next(n for n in diag["nutrients"] if n["key"] == "sodium_mg")
    assert na["status"] == "over_benign"
    assert na["genuine_issue"] is False
    assert "sodium_mg" not in diag["ranked_issues"]


# -- attribution ---------------------------------------------------------------
def test_attribution_points_at_the_dominant_food():
    meals = []
    for i in range(1, 8):
        meals.append(meal(f"2026-07-1{i} 13:00", "chouriço", 60, 300, 10,
                          grounded(saturated_fat_g=25)))       # over the 20 g ceiling
        meals.append(meal(f"2026-07-1{i} 20:00", "alface", 100, 20, 1,
                          grounded(saturated_fat_g=0.2)))
    days = [day(f"2026-07-1{i}", calories=320, protein_g=11, saturated_fat_g=25.2)
            for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    sat = next(n for n in diag["nutrients"] if n["key"] == "saturated_fat_g")
    assert sat["attribution"][0]["food"] == "chouriço"
    assert sat["attribution"][0]["pct"] >= 90


# -- trend ---------------------------------------------------------------------
def test_trend_reads_in_the_helpful_direction():
    # reach: more omega-3 than last week is improving.
    assert insights._trend(1.4, 0.9, "reach") == "improving"
    assert insights._trend(0.9, 1.4, "reach") == "declining"
    # limit: less sodium than last week is improving.
    assert insights._trend(2000, 2600, "limit") == "improving"
    assert insights._trend(2600, 2000, "limit") == "declining"
    assert insights._trend(2000, 2050, "limit") == "steady"


# -- portion math --------------------------------------------------------------
def test_portion_range_closes_the_gap():
    # need 1.6 g omega-3; salmon ~0.02 g/g → ~80 g, within serving bounds.
    rng = insights.portion_range(1.6, 0.02, 2.0, calorie_budget=800)
    assert rng is not None
    low, high = rng
    assert 40 <= low <= high <= 300


def test_portion_range_respects_the_calorie_budget():
    # a calorie-dense food with almost no budget left can't yield a real serving.
    assert insights.portion_range(1.6, 0.02, 8.0, calorie_budget=100) is None


def test_portion_range_none_without_density():
    assert insights.portion_range(1.6, 0.0, 2.0, calorie_budget=800) is None


# -- food profile --------------------------------------------------------------
def test_food_profile_aggregates_and_categorises():
    meals = [
        meal("2026-07-11 08:00", "ovos", 100, 150, 13, grounded(vitamin_d_ug=2.0)),
        meal("2026-07-12 08:00", "ovos", 120, 180, 15, grounded(vitamin_d_ug=2.4)),
        meal("2026-07-12 13:00", "brócolos", 150, 50, 4, grounded()),
    ]
    profile = insights.build_food_profile(meals, ["vitamin_d_ug"])
    eggs = next(f for f in profile if f["food"] == "ovos")
    assert eggs["times_eaten"] == 2
    assert eggs["category"] == "protein_animal"
    assert eggs["top_slot"] == "breakfast"
    assert eggs["median_portion_g"] == 110
    assert eggs["density_per_g"]["vitamin_d_ug"] > 0     # (2.0+2.4)/(100+120)
    veg = next(f for f in profile if f["food"] == "brócolos")
    assert veg["category"] == "vegetable"


# -- end to end ----------------------------------------------------------------
def test_build_diagnosis_shape_and_adherence():
    meals = [meal(f"2026-07-1{i} 13:00", "frango c arroz", 350, 600, 45, grounded())
             for i in range(1, 8)]
    days = [day(f"2026-07-1{i}", calories=1900, protein_g=145, carbs_g=200, fat_g=60,
                fiber_g=32) for i in range(1, 8)]
    diag = insights.build_diagnosis(
        ref_day="2026-07-19", window_days=7, days=days, prev_days=[],
        window_meals=meals, targets=TARGETS, basis=BASIS, policy=POLICY)
    assert diag["window"]["days_logged"] == 7
    assert diag["window"]["meals_logged"] == 7
    prot = diag["adherence"]["protein_g"]
    assert prot["days_hit"] == 7                 # 145 >= 140 every day
    assert prot["per_kg"] == round(145 / 70, 2)
    # protein comfortably on target → a win, and not a ranked issue.
    assert "protein_g" not in diag["ranked_issues"]
    assert any(w["key"] == "protein_g" for w in diag["wins"])


def test_next_meal_context_targets_the_gap():
    profile = [
        {"food": "salmão", "category": "protein_animal", "times_eaten": 3,
         "density_per_g": {"omega3_g": 0.02}, "cal_per_g": 2.0},
        {"food": "alface", "category": "vegetable", "times_eaten": 5,
         "density_per_g": {"omega3_g": 0.0001}, "cal_per_g": 0.15},
    ]
    ctx = insights.next_meal_context(
        consumed={"calories": 1200, "protein_g": 90, "omega3_g": 0.3},
        targets=TARGETS, focus_key="omega3_g", food_profile=profile, slot="dinner")
    assert ctx["calories_left"] == 2100 - 1200
    assert "omega3_g" in ctx["candidates"]
    # the densest source the user eats leads.
    assert ctx["candidates"]["omega3_g"][0]["food"] == "salmão"
