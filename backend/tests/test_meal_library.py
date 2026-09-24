"""Repeating and editing meals: the pure arithmetic and the habit detection.

The two promises tested here are the ones the feature exists for. An edited portion
moves EVERY number with it — macros and micronutrients alike — so a correction is
recorded, not just relabelled. And the meals offered back are the user's actual
habits, recognised through the day-to-day variation (10 g of whey instead of 20, a
banana some days) that made frozen templates useless.
"""
import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

meal_library = importlib.import_module("meal_library")
food_taxonomy = importlib.import_module("food_taxonomy")

TZ = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 9, 24, 9, 30, tzinfo=TZ)
canonical = food_taxonomy.canonical_name


def _item(name, grams, kcal, protein=0.0, nutrients=None, **extra):
    item = {"name": name, "portion_g": float(grams), "calories": float(kcal),
            "protein_g": float(protein), "carbs_g": 0.0, "fat_g": 0.0, **extra}
    if nutrients:
        item["nutrients"] = nutrients
    return item


def _meal(days_ago, hhmm, *items):
    hour, minute = map(int, hhmm.split(":"))
    stamp = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=minute,
                                                     second=0, microsecond=0)
    return {"datetime": stamp.isoformat(timespec="seconds"), "items": list(items)}


OATS = _item("fine rolled oats", 55, 209, 7.3, {"fiber_g": 5.5, "iron_mg": 2.4})
WHEY = _item("whey protein powder", 20, 78, 16)
PB = _item("peanut butter", 10, 59, 2.5)
BANANA = _item("banana", 110, 98, 1.2, {"potassium_mg": 394.0})


# -- rescale: the portion edit ---------------------------------------------------
def test_rescale_moves_macros_and_every_nutrient_in_proportion():
    half = meal_library.rescale(OATS, 27.5)
    assert half["portion_g"] == 27.5
    assert half["calories"] == 104.5 and half["protein_g"] == 3.6
    assert half["nutrients"] == {"fiber_g": 2.75, "iron_mg": 1.2}
    assert OATS["portion_g"] == 55  # the source is never mutated


def test_rescale_drops_a_nutrient_that_rounds_to_nothing():
    item = _item("cooked pasta", 300, 420, 15, {"vitamin_b1_mg": 0.1, "iron_mg": 3.6})
    small = meal_library.rescale(item, 30)
    assert small["nutrients"] == {"iron_mg": 0.4}


def test_rescale_leaves_a_weightless_item_alone():
    capsule = _item("magnesium supplement", 0, 0, nutrients={"magnesium_mg": 200.0})
    assert meal_library.rescale(capsule, 50) == capsule


def test_rescale_ignores_junk_and_an_unchanged_portion():
    assert meal_library.rescale(OATS, "abc") == OATS
    assert meal_library.rescale(OATS, 55) == OATS


def test_override_sets_only_the_macros_it_is_given():
    fixed = meal_library.override(WHEY, {"protein_g": 15, "calories": "70",
                                         "fat_g": -3, "nutrients": {"x": 1}})
    assert fixed["protein_g"] == 15.0 and fixed["calories"] == 70.0
    assert fixed["fat_g"] == 0.0            # clamped, never negative
    assert "nutrients" not in fixed         # a panel can't be typed by hand
    assert fixed["carbs_g"] == WHEY["carbs_g"]


def test_placeholder_is_zero_valued_and_recognisable():
    ph = meal_library.placeholder("banana 120 g")
    assert meal_library.is_placeholder(ph)
    assert all(ph[k] == 0 for k in meal_library.MACRO_KEYS)
    assert not meal_library.is_placeholder(BANANA)


# -- habits ------------------------------------------------------------------------
def test_items_match_on_most_words_but_not_on_one_shared_word():
    assert meal_library.similarity(["fine rolled oat"], ["rolled oat"]) == 1.0
    assert meal_library.similarity(["peanut butter"], ["butter"]) == 0.0


def test_similarity_is_jaccard_over_fuzzy_matches():
    usual = ["rolled oat", "whey protein", "peanut butter", "banana"]
    assert meal_library.similarity(usual, usual[:3]) == 0.75
    assert meal_library.similarity(usual, ["rolled oat", "whey protein", "apple"]) == 0.4
    assert meal_library.similarity([], usual) == 0.0


def test_the_usual_breakfast_is_one_habit_despite_its_variations():
    meals = [
        _meal(1, "09:20", OATS, _item("whey protein", 10, 39, 8), BANANA),
        _meal(2, "09:30", OATS, WHEY, PB, BANANA),
        _meal(3, "09:00", OATS, WHEY, PB),
        _meal(1, "13:00", _item("roast chicken sandwich", 230, 480, 30)),  # one-off
    ]
    habits = meal_library.families(meals, now=NOW, canonical=canonical)
    assert len(habits) == 1
    habit = habits[0]
    assert habit["count"] == 3
    # the offer is the LATEST version — the best guess at today's
    assert habit["meals"][0] is meals[0]
    assert habit["versions"][0] is meals[0]
    assert habit["typical_time"] in ("09:16", "09:17")


def test_versions_skip_repeats_of_the_same_grams():
    meals = [_meal(d, "09:30", OATS, WHEY, BANANA) for d in (1, 2, 3)]
    meals.append(_meal(4, "09:30", OATS, WHEY, meal_library.rescale(BANANA, 60)))
    habit = meal_library.families(meals, now=NOW, canonical=canonical)[0]
    assert habit["count"] == 4
    assert [m["datetime"] for m in habit["versions"]] == [
        meals[0]["datetime"], meals[3]["datetime"]]


def test_time_of_day_decides_between_equally_frequent_habits():
    breakfast = [_meal(d, "09:00", OATS, WHEY) for d in (1, 2)]
    dinner = [_meal(d, "20:30", _item("white rice", 200, 260),
                    _item("turkey steak", 150, 200, 40)) for d in (1, 2)]
    morning = meal_library.families(breakfast + dinner, now=NOW, canonical=canonical)
    evening = meal_library.families(breakfast + dinner,
                                    now=NOW.replace(hour=20), canonical=canonical)
    assert morning[0]["meals"][0] is breakfast[0]
    assert evening[0]["meals"][0] is dinner[0]


def test_recent_habits_outrank_old_ones():
    old = [_meal(d, "09:00", OATS, WHEY) for d in (60, 61, 62)]
    new = [_meal(d, "09:00", _item("baguette", 65, 170), _item("ham", 30, 35))
           for d in (1, 2)]
    habits = meal_library.families(old + new, now=NOW, canonical=canonical)
    assert habits[0]["meals"][0] is new[0]


def test_placeholders_and_undated_rows_never_form_habits():
    pending = meal_library.placeholder("banana 120 g")
    meals = [_meal(1, "10:00", pending), _meal(2, "10:00", pending),
             {"datetime": "not a date", "items": [OATS]},
             {"datetime": "", "items": [OATS]}]
    assert meal_library.families(meals, now=NOW, canonical=canonical) == []


def test_typical_time_wraps_around_midnight():
    meals = [_meal(1, "23:50", _item("greek yogurt", 110, 100)),
             _meal(2, "00:10", _item("greek yogurt", 110, 100))]
    assert meal_library.families(meals, now=NOW, canonical=canonical)[0][
        "typical_time"] == "00:00"


# -- the ingredient library -------------------------------------------------------
def test_ingredients_are_ranked_by_use_with_the_latest_version_as_basis():
    newer_whey = _item("whey protein", 18, 70, 14)
    meals = [_meal(3, "09:00", OATS, WHEY), _meal(1, "09:00", OATS, newer_whey),
             _meal(2, "13:00", _item("white rice", 200, 260))]
    library = meal_library.ingredients(meals, canonical=canonical)
    by_key = {entry["key"]: entry for entry in library}
    assert by_key["whey protein"]["count"] == 2
    assert by_key["whey protein"]["item"] is newer_whey  # latest, not first
    assert library[-1]["key"] == "white rice"             # used once: last
    assert by_key["whey protein"]["last"] == meals[1]["datetime"]


def test_ingredients_skip_placeholders_and_empty_items():
    meals = [_meal(1, "09:00", meal_library.placeholder("banana 120 g"),
                   _item("water", 500, 0),
                   _item("vitamin c", 0, 0, nutrients={"vitamin_c_mg": 500.0}))]
    keys = [e["key"] for e in meal_library.ingredients(meals, canonical=canonical)]
    assert keys == [canonical("vitamin c")]   # a supplement is worth offering
