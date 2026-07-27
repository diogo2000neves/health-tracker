"""Unit tests for the Coach (v2): taxonomy, food patterns, feed, memory, store.

Pure functions over meal rows — no sheet, no bucket, no model, no credentials. The
coach's whole claim is that a human could check its reasoning, so the checkable part
is pinned here: what a food is, how many servings of a group a week is, when an
observation is allowed to fire, and — the one that matters most — that a suggestion
can never reference a food the user has not actually eaten.

The store tests run against its local-directory backend (`COACH_BUCKET` unset), so
the same code path the service uses is exercised without Cloud Storage.
"""
import pathlib
import sys
from datetime import date, datetime, timedelta

import pytest

# The coach modules import each other by name (in the container they are flattened
# into /app together), so the ingest directory goes on the path.
_INGEST = pathlib.Path(__file__).resolve().parent.parent / "ingest"
if str(_INGEST) not in sys.path:
    sys.path.insert(0, str(_INGEST))

import coach_feed as feed            # noqa: E402
import coach_memory as memory        # noqa: E402
import coach_store as store          # noqa: E402
import food_patterns as patterns     # noqa: E402
import food_taxonomy as tax          # noqa: E402


# -- fixtures ------------------------------------------------------------------

def item(name, grams, calories=100, **nutrients):
    return {"name": name, "portion_g": grams, "calories": calories,
            "nutrients": nutrients}


def meal(when, foods, items, calories=500, protein=30):
    return {"datetime": when, "foods": foods, "items": items,
            "calories": calories, "protein_g": protein}


def a_week_of_meals():
    """Seven logged days built to look like the real log this feature was rebuilt
    for: white rice at lunch most days, fries and processed meat, no fish at all,
    vegetables at barely any meal."""
    rows = []
    for offset in range(7):
        day = (datetime(2026, 7, 19) + timedelta(days=offset)).date().isoformat()
        rows.append(meal(f"{day}T08:15:00", "oats, peanut butter",
                         [item("oats", 80), item("peanut butter", 20)]))
        rows.append(meal(f"{day}T13:00:00", "white rice, beef steak",
                         [item("white rice", 150), item("beef steak", 120)]))
        if offset % 2 == 0:
            rows.append(meal(f"{day}T20:30:00", "french fries, cooked ham",
                             [item("french fries", 140), item("sliced ham", 40)]))
        else:
            rows.append(meal(f"{day}T20:30:00", "boiled potato, fried egg",
                             [item("boiled potato", 150), item("fried egg", 50)]))
    return rows


def profile_from(rows, ref_day="2026-07-26"):
    return patterns.build_food_profile(rows, taxonomy=None, window_days=28,
                                       ref_day=ref_day)


# -- taxonomy ------------------------------------------------------------------

class TestCanonicalisation:
    def test_cooking_methods_and_parentheticals_collapse(self):
        """The bug that made every food-level rule read as zero: one food logged
        under four names counted as four foods eaten once each."""
        names = ["cod", "boiled cod", "boiled cod (bacalhau)", "grilled cod"]
        assert len({tax.canonical_name(n) for n in names}) == 1

    def test_brands_and_qualifiers_collapse(self):
        assert (tax.canonical_name("pingo doce freshly squeezed orange juice")
                == tax.canonical_name("freshly squeezed orange juice"))
        assert (tax.canonical_name("high-protein lactose-free skimmed milk")
                == tax.canonical_name("milk"))

    def test_portuguese_and_english_are_one_food(self):
        assert tax.canonical_name("arroz branco") == tax.canonical_name("white rice")
        assert tax.canonical_name("bife de vaca") == tax.canonical_name("beef steak")

    def test_plurals_do_not_mangle_words(self):
        """A blanket "es" -> "" rule read "vegetables" as "vegetabl", which then
        matched no keyword at all; "beans" became "beam"."""
        assert tax.normalize("mixed vegetables") == "vegetable"
        assert tax.normalize("black beans") == "black bean"
        assert tax.normalize("angus beef") == "angus beef"

    def test_whole_is_never_stripped(self):
        """Whole vs refined is the most important distinction here, and it lives in
        exactly this word."""
        assert tax.group_by_rules(tax.canonical_name("whole grain bread")) == "whole_grain"
        assert tax.group_by_rules(tax.canonical_name("white bread")) == "refined_grain"


class TestDisplayNames:
    """The app is pt-PT end to end while the log is keyed in English. These are the
    rules that decide which name the user actually reads."""

    def test_the_meals_own_name_pt_wins_over_everything(self):
        """Written at ingest against the photo AND the user's note, so it is the only
        source that can know the dish was a francesinha and not "a sandwich"."""
        assert tax.display_pt("portuguese sandwich",
                              name_pt="francesinha") == "francesinha"

    def test_the_curated_table_covers_the_common_vocabulary(self):
        assert tax.display_pt("white rice") == "arroz branco"
        assert tax.display_pt("cod") == "bacalhau"
        assert tax.display_pt("boiled cod (bacalhau)") == "bacalhau"

    def test_a_name_already_in_portuguese_is_left_alone(self):
        assert tax.display_pt("arroz branco") == "arroz branco"

    def test_brands_and_english_loanwords_are_not_translated(self):
        """"proteína de soro de leite" is not what anyone in Portugal says."""
        assert tax.display_pt("whey protein") == "whey protein"
        assert tax.display_pt("Big Tasty") == "Big Tasty"

    def test_an_unknown_food_falls_back_to_its_english_name(self):
        """A missing translation must never blank a meal — the worst case is a food
        that reads in the wrong language, never a food that isn't there."""
        assert tax.display_pt("mystery gruel") == "mystery gruel"

    def test_the_learned_blob_fills_what_the_table_cannot(self):
        blob = {"pt": {tax.display_key("skin-on chicken thigh"):
                       "coxa de frango com pele"}}
        assert tax.display_pt("skin-on chicken thigh",
                              blob) == "coxa de frango com pele"

    def test_lookup_keeps_the_logged_detail_and_the_bucket_apart(self):
        """The canonical name has had the cooking method stripped for grouping, so
        showing it back would quietly drop detail the app is meant to display."""
        blob = {"pt": {tax.display_key("grilled chicken breast"):
                       "peito de frango grelhado"}}
        info = tax.lookup(blob, "grilled chicken breast")
        assert info["canonical"] == "chicken breast"
        assert info["pt"] == "peito de frango grelhado"
        assert info["pt_canonical"] == "peito de frango"


class TestTranslationLearning:
    def test_only_names_nothing_can_place_are_worth_a_call(self):
        names = ["white rice", "whey protein", "mystery gruel", "Mystery Gruel"]
        # The first two resolve from the curated table; the last two are one food.
        assert tax.untranslated_names(None, names) == ["mystery gruel"]

    def test_a_learned_name_is_folded_in_and_stops_being_asked_about(self):
        blob, learned = tax.translate_unknown(
            None, ["mystery gruel"],
            lambda _p: {"foods": [{"name": "mystery gruel", "pt": "papa misteriosa"}]})
        assert learned == 1
        assert tax.display_pt("mystery gruel", blob) == "papa misteriosa"
        assert tax.untranslated_names(blob, ["mystery gruel"]) == []

    def test_an_answer_for_a_name_we_never_asked_about_is_dropped(self):
        """A hallucinated key would otherwise sit in the lexicon forever, renaming a
        food nobody ever logged."""
        blob, learned = tax.translate_unknown(
            None, ["mystery gruel"],
            lambda _p: {"foods": [{"name": "caviar", "pt": "caviar"}]})
        assert learned == 0 and blob["pt"] == {}

    def test_a_failed_call_leaves_the_english_name_standing(self):
        def _boom(_prompt):
            raise RuntimeError("no model today")

        blob, learned = tax.translate_unknown(None, ["mystery gruel"], _boom)
        assert learned == 0
        assert tax.display_pt("mystery gruel", blob) == "mystery gruel"


class TestGrouping:
    @pytest.mark.parametrize("name,group", [
        ("pear", "fruit"),                    # not a legume, despite "pea"
        ("peanut butter", "nut_seed"),        # nor this
        ("potato chips", "fried_potato"),     # not a grain, despite "potato"
        ("french fries", "fried_potato"),
        ("boiled potato", "potato"),
        ("sliced ham", "processed_meat"),     # not red meat
        ("beef steak", "red_meat"),
        ("chicken breast", "poultry"),
        ("boiled cod (bacalhau)", "fish_white"),
        ("salmon nigiri", "fish_oily"),
        ("breaded fried tiger shrimp", "seafood"),
        ("black beans", "legume"),
        ("oats", "whole_grain"),
        ("white rice", "refined_grain"),
        ("olive oil", "fat_healthy"),
        ("butter", "fat_sat"),
        ("whey protein powder", "protein_supplement"),
        ("pale lager beer", "alcohol"),
        ("orange juice", "sugary_drink"),
        ("mixed salad", "vegetable"),
    ])
    def test_real_log_names_land_in_the_right_group(self, name, group):
        assert tax.lookup(None, name)["group"] == group

    def test_frying_is_read_from_the_raw_name(self):
        assert tax.lookup(None, "fried egg")["fried"] is True
        assert tax.lookup(None, "boiled egg")["fried"] is False
        # ...and the canonical name still ignores the method.
        assert tax.lookup(None, "fried egg")["canonical"] == "egg"

    def test_learned_taxonomy_only_fills_gaps(self):
        learned = {"foods": {"seitan": {"canonical": "seitan", "group": "legume"},
                             "white rice": {"canonical": "white rice",
                                            "group": "sweet"}}}
        assert tax.lookup(learned, "seitan")["group"] == "legume"
        # A model answer can never override the curated rules.
        assert tax.lookup(learned, "white rice")["group"] == "refined_grain"

    def test_unknown_names_are_the_only_ones_offered_to_the_model(self):
        names = ["white rice", "seitan", "tempeh", "cod"]
        assert set(tax.unknown_names(None, names)) == {"seitan", "tempeh"}

    def test_classification_failure_is_not_fatal(self):
        def boom(_prompt):
            raise RuntimeError("gemini down")
        result, learned = tax.classify_unknown(None, ["seitan"], boom)
        assert learned == 0 and result["foods"] == {}


class TestServings:
    def test_portion_scales_but_one_meal_is_capped(self):
        assert tax.servings("fish_white", 130) == pytest.approx(1.0)
        assert tax.servings("fish_white", 260) == pytest.approx(2.0)
        assert tax.servings("fish_white", 900) == pytest.approx(2.0)

    def test_a_sliver_is_not_a_serving(self):
        assert tax.servings("fish_white", 20) == 0.0


# -- food patterns -------------------------------------------------------------

class TestGroupStats:
    def test_servings_are_normalised_to_a_week(self):
        stats = profile_from(a_week_of_meals())["groups"]
        # White rice, 150 g at lunch on all seven logged days. That is 2.5 reference
        # servings a day, but one occurrence counts at most 2 — so 14 a week, not
        # 17.5: a single big plate must not read as a week's worth.
        assert stats["refined_grain"]["servings_per_week"] == pytest.approx(14.0,
                                                                           abs=0.1)
        assert stats["red_meat"]["occurrences_per_week"] == pytest.approx(7, abs=0.1)

    def test_absent_positive_groups_are_materialised_at_zero(self):
        """"No fish in the window" is the single most useful thing here, and it lives
        in the absence of a row — so the row has to exist."""
        stats = profile_from(a_week_of_meals())["groups"]
        assert stats["fish_white"]["servings_per_week"] == 0.0
        assert stats["fish_white"]["days_since_last"] is None
        assert "sweet" not in stats            # a `less` group that never appeared

    def test_days_since_last_is_measured_from_the_reference_day(self):
        rows = a_week_of_meals()
        stats = patterns.build_food_profile(rows, taxonomy=None, window_days=28,
                                           ref_day="2026-07-28")["groups"]
        # The last logged day is 2026-07-25.
        assert stats["red_meat"]["days_since_last"] == 3


class TestStreaks:
    def test_a_routine_is_counted_over_logged_days_not_calendar_days(self):
        """A gap in the log is not evidence that the habit broke."""
        rows = [meal("2026-07-19T13:00:00", "white rice", [item("white rice", 150)]),
                meal("2026-07-20T13:00:00", "white rice", [item("white rice", 150)]),
                # 21st and 22nd not logged at all
                meal("2026-07-23T13:00:00", "white rice", [item("white rice", 150)])]
        found = patterns.streaks(patterns.read_meals(rows, None),
                                ref_day="2026-07-26")
        assert found and found[0]["food"] == "white rice"
        assert found[0]["days"] == 3

    def test_the_same_food_at_a_different_meal_is_not_a_streak(self):
        rows = [meal("2026-07-19T13:00:00", "rice", [item("white rice", 150)]),
                meal("2026-07-20T20:30:00", "rice", [item("white rice", 150)]),
                meal("2026-07-21T13:00:00", "rice", [item("white rice", 150)])]
        assert patterns.streaks(patterns.read_meals(rows, None),
                                ref_day="2026-07-26") == []


class TestSlotAndVariety:
    def test_slot_composition_counts_plants_and_protein(self):
        slots = profile_from(a_week_of_meals())["slots"]
        assert slots["lunch"]["meals"] == 7
        assert slots["lunch"]["plant_pct"] == 0.0        # rice + beef, nothing else
        assert slots["lunch"]["protein_pct"] == 1.0
        assert slots["breakfast"]["typical_hour"] == 8

    def test_repetition_is_the_share_of_the_top_five_foods(self):
        variety = profile_from(a_week_of_meals())["variety"]
        assert variety["distinct_vegetables"] == 0
        assert variety["top_share"] >= 0.45
        assert variety["days_logged"] == 7


class TestFindings:
    def test_a_thin_log_produces_no_findings(self):
        """With three logged days, "no fish this week" is a gap in the log, not a gap
        in the diet — and inventing urgency from it is how a coach loses trust."""
        rows = a_week_of_meals()[:6]           # two days
        assert profile_from(rows)["findings"] == []

    def test_missing_fish_and_legumes_are_reported(self):
        found = profile_from(a_week_of_meals())["findings"]
        under = {f["group"] for f in found if f["kind"] == "group_under"}
        assert {"fish_oily", "fish_white", "legume", "vegetable"} <= under

    def test_excess_groups_are_reported_with_their_reference(self):
        found = profile_from(a_week_of_meals())["findings"]
        over = {f["group"]: f for f in found if f["kind"] == "group_over"}
        assert "red_meat" in over
        assert over["red_meat"]["evidence"]["reference_max"] == 3
        assert over["red_meat"]["evidence"]["servings_per_week"] > 3

    def test_refined_share_is_a_ratio_not_a_count(self):
        """Eating a lot of grain is not the observation; eating almost no *whole*
        grain is. The oats-every-morning log below must NOT trigger it."""
        assert "refined_share" not in {
            f["kind"] for f in profile_from(a_week_of_meals())["findings"]}

        # Swap the daily oats for white bread and it should fire.
        white_bread_instead = [
            meal(row["datetime"], "white bread, butter",
                 [item("white bread", 80), item("butter", 10)])
            if row["datetime"][11:13] == "08" else row
            for row in a_week_of_meals()]
        found = {f["kind"] for f in profile_from(white_bread_instead)["findings"]}
        assert "refined_share" in found

    def test_every_finding_carries_its_evidence(self):
        for finding in profile_from(a_week_of_meals())["findings"]:
            assert finding["headline"] and isinstance(finding["evidence"], dict)
            assert finding["evidence"], f"{finding['id']} has no evidence"

    def test_findings_are_ordered_by_severity(self):
        severities = [f["severity"]
                      for f in profile_from(a_week_of_meals())["findings"]]
        assert severities == sorted(severities, reverse=True)


class TestSwapCandidates:
    def test_replacements_prefer_foods_already_eaten(self):
        profile = profile_from(a_week_of_meals())
        over = next(f for f in profile["findings"]
                    if f["kind"] == "group_over" and f["group"] == "red_meat")
        swaps = patterns.swap_candidates(over, profile["foods"])
        assert [f["food"] for f in swaps["from"]]           # only logged foods
        assert all(f["food"] in {x["food"] for x in profile["foods"]}
                   for f in swaps["from"])
        # This log has no fish at all, so fish arrives as an explicitly-new staple
        # rather than a pretend habit.
        fish = [t for t in swaps["to"] if t["group"] in tax.FISH_GROUPS]
        assert fish and all(t["new"] for t in fish)

    def test_a_group_the_user_does_eat_is_offered_as_theirs(self):
        rows = a_week_of_meals() + [
            meal(f"2026-07-2{d}T20:30:00", "cod", [item("boiled cod", 150)])
            for d in (0, 2, 4)]
        profile = profile_from(rows)
        finding = {"kind": "group_over", "group": "red_meat", "id": "x"}
        profile_swaps = patterns.swap_candidates(finding, profile["foods"])
        cod = [t for t in profile_swaps["to"] if t["food"] == "cod"]
        assert cod and cod[0]["new"] is False


# -- the feed ------------------------------------------------------------------

NOW = datetime(2026, 7, 26, 15, 30)


def generate(slot, profile, answer, *, now=NOW, today=None, state=None):
    """Drive one generation exactly the way the endpoint does: build the facts, pull
    the findings index out of them, then assemble the model's answer against it."""
    today = today if today is not None else {"meals": [], "calories_left": 900}
    facts = feed.build_generation_facts(
        slot=slot, now=now, profile=profile, today=today, nutrients={},
        memory={}, state=state or {})
    findings = list(facts.pop("_findings_index", {}).values())
    return feed.assemble(answer, slot=slot, now=now, profile=profile, today=today,
                         findings=findings)


def an_answer(cards=(), plates=True):
    out = {"cards": list(cards)}
    if plates:
        out["next_meal"] = {
            "next_slot": "jantar", "reasoning": "Já almoçaste.",
            "rationale": "Faltam-te 900 kcal e a semana pede peixe.",
            "plates": [{"rank": 1, "recommended": True, "title": "Bacalhau com grão",
                        "items": [{"food": "cod", "grams_low": 130,
                                   "grams_high": 160, "new": False}],
                        "calories": 520, "protein_g": 42, "why": "Fecha a semana."}]}
    return out


class TestCardAssembly:
    def test_ids_are_stable_so_a_rerun_replaces_its_own_cards(self):
        first = feed.card_id(date="2026-07-26", kind="check_in")
        again = feed.card_id(date="2026-07-26", kind="check_in")
        assert first == again

    def test_the_same_question_asked_by_two_slots_is_one_card(self):
        """"What do I eat next" has one current answer. Keyed by slot, the 15:30 run
        and the refresh triggered by a logged lunch would both sit in the feed, two
        cards with the same title saying different things."""
        profile = profile_from(a_week_of_meals())
        afternoon, _ = generate("afternoon", profile, an_answer())
        adhoc, _ = generate("adhoc", profile, an_answer())
        merged = feed.merge_cards(afternoon, adhoc, now=NOW)
        assert len([c for c in merged if c["kind"] == "next_meal"]) == 1

    def test_a_rerun_does_not_stack_duplicates(self):
        profile = profile_from(a_week_of_meals())
        card = {"kind": "check_in", "title": "Olá", "body": "Corpo do cartão."}
        one, _ = generate("afternoon", profile, an_answer([card], plates=False))
        two, _ = generate("afternoon", profile, an_answer([card], plates=False))
        assert len(feed.merge_cards(one, two, now=NOW)) == 1

    def test_expired_cards_drop_out_of_the_feed(self):
        stale = {"id": "old", "kind": "pattern", "priority": 90,
                 "created_at": "2026-07-20T08:00:00",
                 "expires_at": "2026-07-21T08:00:00"}
        live = {"id": "new", "kind": "pattern", "priority": 50,
                "created_at": "2026-07-26T08:00:00",
                "expires_at": "2026-07-27T08:00:00"}
        assert [c["id"] for c in feed.live_cards([stale, live], now=NOW)] == ["new"]

    def test_feed_order_is_priority_then_recency(self):
        cards = [
            {"id": "a", "priority": 60, "created_at": "2026-07-26T08:00:00"},
            {"id": "b", "priority": 100, "created_at": "2026-07-26T07:00:00"},
            {"id": "c", "priority": 100, "created_at": "2026-07-26T09:00:00"},
        ]
        assert [c["id"] for c in feed.feed_order(cards)] == ["c", "b", "a"]

    def test_every_card_gets_a_stable_chat_thread(self):
        profile = profile_from(a_week_of_meals())
        cards, _ = generate("afternoon", profile,
                            an_answer([{"kind": "check_in", "title": "T",
                                        "body": "B"}], plates=False))
        assert cards[0]["thread_id"] == feed.thread_id_for(cards[0])
        assert store.is_safe_id(cards[0]["thread_id"])

    def test_the_next_meal_card_carries_its_rationale(self):
        """"Why these, for me, right now" — the plates alone never answered it."""
        profile = profile_from(a_week_of_meals())
        cards, _ = generate("afternoon", profile, an_answer())
        meal_card = next(c for c in cards if c["kind"] == "next_meal")
        assert meal_card["body"] == "Faltam-te 900 kcal e a semana pede peixe."
        # The slot reasoning is kept, but as evidence rather than as the headline.
        assert meal_card["evidence"]["reasoning"] == "Já almoçaste."

    def test_a_missing_rationale_falls_back_to_the_reasoning(self):
        profile = profile_from(a_week_of_meals())
        answer = an_answer()
        answer["next_meal"].pop("rationale")
        cards, _ = generate("afternoon", profile, answer)
        meal_card = next(c for c in cards if c["kind"] == "next_meal")
        assert meal_card["body"] == "Já almoçaste."


class TestContextualRelevance:
    """Opening the app after dinner must lead with the day's whole story, not with
    this morning's read on breakfast."""

    def _card(self, kind, hour):
        moment = NOW.replace(hour=hour)
        return feed._card(kind=kind, slot="adhoc", date="2026-07-26", now=moment,
                          title="T", body="B")

    def test_a_morning_check_in_is_not_shown_in_the_evening(self):
        morning = self._card("check_in", 9)
        assert feed.relevant_now(morning, now=NOW.replace(hour=21)) is False

    def test_it_is_shown_while_the_afternoon_lasts(self):
        afternoon = self._card("check_in", 15)
        assert feed.relevant_now(afternoon, now=NOW.replace(hour=17)) is True

    def test_a_lunch_suggestion_is_not_an_answer_at_night(self):
        lunch = self._card("next_meal", 13)
        assert feed.relevant_now(lunch, now=NOW.replace(hour=21)) is False

    def test_habits_are_always_relevant(self):
        for kind in ("pattern", "win", "weekly_review"):
            card = self._card(kind, 9)
            assert feed.relevant_now(card, now=NOW.replace(hour=23)) is True

    def test_the_small_hours_still_belong_to_last_night(self):
        """At 00:30 the useful thing is yesterday's summary, not a plan for a day the
        user hasn't started."""
        assert feed.slot_for(NOW.replace(hour=0, minute=30)) == "evening"
        summary = self._card("day_summary", 22)
        assert feed.relevant_now(summary, now=NOW.replace(hour=0, minute=30)) is True

    def test_a_feed_with_nothing_about_now_asks_to_be_refreshed(self):
        morning = self._card("check_in", 9)
        assert feed.context_stale([morning], now=NOW.replace(hour=21)) is True
        evening = self._card("day_summary", 21)
        assert feed.context_stale([morning, evening],
                                  now=NOW.replace(hour=21)) is False

    def test_habits_alone_do_not_satisfy_the_moment(self):
        """A feed of nothing but pattern cards has nothing to say about right now."""
        pattern = self._card("pattern", 9)
        assert feed.context_stale([pattern], now=NOW) is True


class TestSwapValidation:
    """The guard that replaces a prompt rule the model demonstrably broke in
    production: it proposed swapping "pão branco" for a user who had never logged
    any bread."""

    def setup_method(self):
        self.profile = profile_from(a_week_of_meals())
        self.finding = feed.eligible_findings(self.profile, {},
                                              today="2026-07-26")[0]
        offered = self.profile["swaps"][self.finding["id"]]["to"][0]
        self.offered = offered["food"]            # the English key, as stored
        self.offered_pt = offered.get("pt") or offered["food"]  # as the model sees it

    def _run(self, swap):
        cards, _ = generate("afternoon", self.profile, an_answer([
            {"kind": "pattern", "ref": self.finding["id"], "title": "T",
             "body": "B", "swap": swap}], plates=False))
        return cards[0]["swap"]

    def test_a_swap_from_an_unlogged_food_is_dropped(self):
        assert self._run({"from": "pão branco", "to": self.offered,
                          "why": "porque"}) is None

    def test_a_swap_to_an_unoffered_food_is_dropped(self):
        assert self._run({"from": "beef steak", "to": "caviar",
                          "why": "porque"}) is None

    def test_a_supported_swap_survives_with_its_new_flag(self):
        swap = self._run({"from": "beef steak", "to": self.offered,
                          "why": "porque"})
        # Accepted from the English spelling, but rendered in pt-PT: the card is
        # read inside Portuguese prose, so both sides come back translated.
        assert swap and swap["from"] == "bife de vaca"
        assert swap["to"] == self.offered_pt
        assert isinstance(swap["new"], bool)

    def test_a_swap_phrased_in_portuguese_is_accepted(self):
        """The coach is prompted in Portuguese and shown Portuguese food names, so
        this is the spelling it actually answers with — the validator must not read
        it as a food the user never logged."""
        swap = self._run({"from": "bife de vaca", "to": self.offered_pt,
                          "why": "porque"})
        assert swap and swap["from"] == "bife de vaca"
        assert swap["to"] == self.offered_pt

    def test_a_pattern_card_without_a_real_finding_is_dropped(self):
        cards, shown = generate("afternoon", self.profile, an_answer([
            {"kind": "pattern", "ref": "made:up", "title": "T", "body": "B"}],
            plates=False))
        assert cards == [] and shown == {}


class TestMealContextSwaps:
    """A ham sandwich at breakfast answered with codfish is nutritionally sound and
    practically useless — which is worse than saying nothing."""

    def _breakfast_ham_log(self):
        rows = []
        for offset in range(7):
            day = (datetime(2026, 7, 19) + timedelta(days=offset)).date().isoformat()
            rows.append(meal(f"{day}T08:15:00", "bread, ham, cheese",
                             [item("white bread", 80), item("sliced ham", 40),
                              item("cheese", 30)]))
            rows.append(meal(f"{day}T13:00:00", "rice, cod",
                             [item("white rice", 150), item("boiled cod", 140)]))
            rows.append(meal(f"{day}T20:30:00", "potato, egg",
                             [item("boiled potato", 150), item("fried egg", 50)]))
        return rows

    def test_a_breakfast_food_is_replaced_by_breakfast_food(self):
        profile = profile_from(self._breakfast_ham_log())
        finding = next(f for f in profile["findings"]
                       if f["group"] == "processed_meat")
        swaps = patterns.swap_candidates(finding, profile["foods"])

        assert swaps["replacing_at"] == "breakfast"
        best = swaps["to"][0]
        assert best["fits_the_meal"] is True
        # Cod is logged (at lunch) and is a fine protein — but it must not be the
        # headline answer for a breakfast sandwich.
        assert best["food"] != "cod"

    def test_options_that_fit_the_meal_are_ranked_first(self):
        profile = profile_from(self._breakfast_ham_log())
        finding = next(f for f in profile["findings"]
                       if f["group"] == "processed_meat")
        fits = [t["fits_the_meal"] is True
                for t in patterns.swap_candidates(finding, profile["foods"])["to"]]
        assert fits == sorted(fits, reverse=True)

    def test_a_group_the_user_never_eats_falls_back_to_a_fitting_staple(self):
        """No dairy at all in this log, so the breakfast fallback must still be
        something anyone would eat at breakfast."""
        profile = profile_from(self._breakfast_ham_log())
        finding = {"kind": "group_over", "group": "processed_meat",
                   "id": "group_over:processed_meat",
                   "evidence": {"slot": "breakfast"}}
        options = patterns.swap_candidates(finding, profile["foods"])["to"]
        assert options
        assert all(o["food"] != "cod" or o["fits_the_meal"] is not True
                   for o in options)


class TestPatternCooldown:
    def test_a_shown_finding_stays_quiet_for_its_cooldown(self):
        profile = profile_from(a_week_of_meals())
        top = profile["findings"][0]
        state = {"shown": {top["id"]: {"date": "2026-07-24",
                                       "severity": top["severity"]}}}
        eligible = feed.eligible_findings(profile, state, today="2026-07-26")
        assert top["id"] not in {f["id"] for f in eligible}

    def test_it_comes_back_once_the_cooldown_passes(self):
        profile = profile_from(a_week_of_meals())
        top = profile["findings"][0]
        old = (datetime(2026, 7, 26)
               - timedelta(days=feed.PATTERN_COOLDOWN_DAYS + 1)).date().isoformat()
        state = {"shown": {top["id"]: {"date": old, "severity": top["severity"]}}}
        assert top["id"] in {f["id"] for f in
                             feed.eligible_findings(profile, state,
                                                    today="2026-07-26")}

    def test_it_jumps_the_cooldown_if_it_got_materially_worse(self):
        profile = profile_from(a_week_of_meals())
        top = profile["findings"][0]
        state = {"shown": {top["id"]: {
            "date": "2026-07-25",
            "severity": top["severity"] - feed.SEVERITY_ESCALATION - 0.05}}}
        assert top["id"] in {f["id"] for f in
                             feed.eligible_findings(profile, state,
                                                    today="2026-07-26")}

    def test_generation_reports_what_it_showed(self):
        profile = profile_from(a_week_of_meals())
        top = feed.eligible_findings(profile, {}, today="2026-07-26")[0]
        _cards, shown = generate("morning", profile, an_answer(
            [{"kind": "pattern", "ref": top["id"], "title": "T", "body": "B"}],
            plates=False))
        assert shown[top["id"]]["date"] == "2026-07-26"


class TestCoherence:
    def test_the_model_is_told_what_it_already_said(self):
        """Not just the headlines: "don't repeat yourself" is unenforceable against a
        list of titles alone."""
        profile = profile_from(a_week_of_meals())
        facts = feed.build_generation_facts(
            slot="afternoon", now=NOW, profile=profile, today={"meals": []},
            nutrients={}, memory={}, state={},
            recent=[{"title": "Duas semanas sem peixe",
                     "body": "A carne vermelha apareceu sete vezes."}])
        said = facts["already_said_recently"][0]
        assert said["title"] and said["body"]

    def test_the_plates_and_the_cards_are_asked_for_together(self):
        """The fish/beef contradiction came from two calls that couldn't see each
        other. One set of facts, one answer, one shared context."""
        profile = profile_from(a_week_of_meals())
        facts = feed.build_generation_facts(
            slot="afternoon", now=NOW, profile=profile,
            today={"meals": [], "calories_left": 900}, nutrients={}, memory={},
            state={}, next_meal={"candidates": {"usual_at_this_slot": []}})
        assert facts["wanted_next_meal"] is True
        assert "next_meal" in facts and "wanted_cards" in facts

    def test_the_weekly_review_asks_for_no_plates(self):
        profile = profile_from(a_week_of_meals())
        facts = feed.build_generation_facts(
            slot="weekly", now=NOW, profile=profile, today={"meals": []},
            nutrients={}, memory={}, state={})
        assert facts["wanted_next_meal"] is False


class TestNextMeal:
    def test_there_are_always_candidates_even_with_nothing_short(self):
        """The old generator returned "skipped" when no nutrient was below its floor,
        which left the app's suggestion sheet loading forever with nothing that could
        ever fill it. "What do I eat next?" is always answerable."""
        profile = profile_from(a_week_of_meals())
        candidates = feed.next_meal_candidates(profile, nutrient_candidates={},
                                               slot_hint="dinner")
        assert candidates["usual_at_this_slot"]
        assert candidates["for_findings"] or candidates["groups_to_favour"]

    def test_under_target_groups_come_with_something_concrete(self):
        profile = profile_from(a_week_of_meals())
        favour = feed.next_meal_candidates(
            profile, nutrient_candidates={})["groups_to_favour"]
        assert favour and all(g["options"] for g in favour)

    def test_an_answer_with_no_plates_still_yields_the_other_cards(self):
        profile = profile_from(a_week_of_meals())
        cards, _ = generate("morning", profile, an_answer(
            [{"kind": "day_plan", "title": "T", "body": "B"}], plates=False))
        assert [c["kind"] for c in cards] == ["day_plan"]

    def test_an_answer_with_no_cards_still_yields_the_plates(self):
        profile = profile_from(a_week_of_meals())
        cards, _ = generate("morning", profile, an_answer([]))
        assert [c["kind"] for c in cards] == ["next_meal"]
        assert cards[0]["plates"]


class TestStaleness:
    def test_an_empty_feed_is_stale(self):
        assert feed.is_stale([], now=NOW) is True

    def test_a_fresh_card_is_not_stale(self):
        card = {"created_at": (NOW - timedelta(hours=1)).isoformat()}
        assert feed.is_stale([card], now=NOW, max_age_hours=6) is False

    def test_an_old_card_is_stale(self):
        card = {"created_at": (NOW - timedelta(hours=9)).isoformat()}
        assert feed.is_stale([card], now=NOW, max_age_hours=6) is True

    @pytest.mark.parametrize("hour,slot", [(7, "morning"), (12, "afternoon"),
                                           (15, "afternoon"), (21, "evening")])
    def test_slot_for_the_clock(self, hour, slot):
        assert feed.slot_for(NOW.replace(hour=hour)) == slot


# -- memory --------------------------------------------------------------------

class TestMemory:
    def test_rephrasings_of_one_fact_merge(self):
        mem, added = memory.merge(None, [
            {"type": "dislike", "fact": "não gosta de peixe cozido",
             "confidence": 0.9},
            {"type": "dislike", "fact": "não gosta de peixe cozido ao jantar",
             "confidence": 0.7}], today="2026-07-26")
        assert added == 1 and len(mem["facts"]) == 1
        assert mem["facts"][0]["mentions"] == 2

    def test_a_guessed_fact_is_not_remembered(self):
        _mem, added = memory.merge(None, [{"type": "goal", "fact": "talvez queira",
                                           "confidence": 0.2}],
                                   today="2026-07-26")
        assert added == 0

    def test_a_like_and_a_dislike_never_fold_together(self):
        """These two sentences share almost every word. Merging them would make the
        coach act on the opposite of what the user said."""
        mem, added = memory.merge(None, [
            {"type": "preference", "fact": "gosta de peixe grelhado",
             "confidence": 0.9},
            {"type": "dislike", "fact": "não gosta de peixe cozido",
             "confidence": 0.9}], today="2026-07-26")
        assert added == 2 and len(mem["facts"]) == 2

    def test_memory_is_bounded(self):
        candidates = [{"type": "context", "fact": f"come alimento{i} ao lanche",
                       "confidence": 0.9} for i in range(memory.MAX_FACTS + 15)]
        mem, _ = memory.merge(None, candidates, today="2026-07-26")
        assert len(mem["facts"]) == memory.MAX_FACTS

    def test_what_the_user_says_themselves_is_pinned_and_kept(self):
        mem = memory.add_manual(None, kind="constraint",
                                fact="não come lactose à noite", today="2026-07-26")
        pinned = [f for f in mem["facts"] if f["pinned"]]
        assert len(pinned) == 1 and pinned[0]["source"] == "user"
        # Pinned facts survive pruning even against many louder candidates.
        noisy = [{"type": "context", "fact": f"outro facto {i}", "confidence": 1.0}
                 for i in range(memory.MAX_FACTS + 5)]
        after, _ = memory.merge(mem, noisy, today="2026-07-27")
        assert any(f["pinned"] for f in after["facts"])

    def test_a_wrong_fact_can_be_removed(self):
        mem = memory.add_manual(None, kind="dislike", fact="odeia brócolos",
                                today="2026-07-26")
        fact_id = mem["facts"][0]["id"]
        assert memory.remove(mem, fact_id)["facts"] == []

    def test_the_prompt_view_is_only_the_sentences(self):
        mem = memory.add_manual(None, kind="dislike", fact="odeia brócolos",
                                today="2026-07-26")
        assert memory.for_prompt(mem) == [{"type": "dislike",
                                           "fact": "odeia brócolos"}]


# -- store ---------------------------------------------------------------------

class TestStore:
    @pytest.fixture(autouse=True)
    def _local_bucket(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COACH_BUCKET", raising=False)
        monkeypatch.setenv("COACH_LOCAL_DIR", str(tmp_path))

    def test_round_trip(self):
        store.write_json("coach/feed/2026-07-26.json", {"cards": [{"id": "a"}]})
        assert store.read_json("coach/feed/2026-07-26.json")["cards"][0]["id"] == "a"

    def test_a_missing_blob_reads_as_the_default(self):
        """Every read is on a path the app is waiting on, so nothing here may raise."""
        assert store.read_json("coach/feed/nope.json", default={"cards": []}) == {
            "cards": []}

    def test_a_corrupt_blob_reads_as_the_default(self, tmp_path):
        path = tmp_path / "coach" / "memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert store.read_json(store.MEMORY, default={"facts": []}) == {"facts": []}

    def test_update_applies_the_mutation(self):
        store.update_json("coach/x.json", lambda cur: {"n": (cur or {}).get("n", 0) + 1},
                          default={})
        store.update_json("coach/x.json", lambda cur: {"n": (cur or {}).get("n", 0) + 1},
                          default={})
        assert store.read_json("coach/x.json") == {"n": 2}

    def test_one_generation_runs_at_a_time(self):
        """Two overlapping runs would both spend model calls writing the same cards —
        which is what a foregrounding app used to trigger three times over."""
        assert store.claim_job("j1", "manual", "2026-07-26T15:00:00")
        assert store.claim_job("j2", "manual", "2026-07-26T15:00:30") is None
        store.finish_job("j1", "2026-07-26T15:01:00")
        assert store.claim_job("j3", "manual", "2026-07-26T15:02:00")

    def test_a_crashed_run_never_wedges_the_feature(self):
        store.claim_job("j1", "manual", "2026-07-26T15:00:00")
        # Its owner died without finishing; ten minutes later it must be takeable.
        assert store.claim_job("j2", "manual", "2026-07-26T15:10:00")

    def test_a_live_job_is_what_the_app_shows_progress_for(self):
        store.claim_job("j1", "manual", "2026-07-26T15:00:00")
        state = store.read_state()
        assert store.job_is_live(state, "2026-07-26T15:00:20") is True
        assert store.job_is_live(state, "2026-07-26T15:30:00") is False

    @pytest.mark.parametrize("bad", ["../secrets", "a/b", "", "x" * 200,
                                     "id with spaces"])
    def test_an_id_that_could_address_another_blob_is_rejected(self, bad):
        assert store.is_safe_id(bad) is False

    def test_ordinary_ids_pass(self):
        assert store.is_safe_id("t-9f2c1a")
        assert store.is_safe_id("2026-07-26:afternoon:pattern")


# -- endpoint contracts --------------------------------------------------------
# The full wiring, with the spreadsheet and the model faked out: does a generation
# actually write cards, does the feed read them back, and does the read path stay a
# pure storage read?

import importlib.util                                            # noqa: E402

_MAIN = pathlib.Path(__file__).resolve().parent.parent / "ingest" / "main.py"
_main_spec = importlib.util.spec_from_file_location("ingest_main_coach", _MAIN)
ingest = importlib.util.module_from_spec(_main_spec)
_main_spec.loader.exec_module(ingest)

TARGETS = {
    "calories": {"kind": "window", "floor": 1860, "ceiling": 2200, "unit": "kcal"},
    "protein_g": {"kind": "reach", "floor": 136, "unit": "g"},
    "fiber_g": {"kind": "reach", "floor": 28, "unit": "g"},
}


class TestCoachEndpoints:
    @pytest.fixture(autouse=True)
    def _wired(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COACH_BUCKET", raising=False)
        monkeypatch.setenv("COACH_LOCAL_DIR", str(tmp_path))
        monkeypatch.setenv("INGEST_TOKEN", "tok")
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr(ingest, "_all_meal_rows", a_week_of_meals)
        monkeypatch.setattr(ingest, "_resolved_targets_and_basis",
                            lambda: (TARGETS, {"weight_kg": 68.25}))
        monkeypatch.setattr(ingest, "_tz", lambda: None)
        real_datetime = ingest.datetime

        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 15, 30)
        monkeypatch.setattr(ingest, "datetime", FrozenDatetime)
        # Taxonomy learning must never reach the network in a test.
        narrator_mod = ingest._narrator_mod()
        monkeypatch.setattr(narrator_mod, "call_gemini",
                            lambda *a, **kw: {"foods": []})
        self.client = ingest.app.test_client()
        self.auth = {"X-Auth-Token": "tok"}

    ANSWER = {
        "next_meal": {"next_slot": "jantar", "reasoning": "Já almoçaste.",
                      "rationale": "Faltam-te 900 kcal.",
                      "plates": [{"rank": 1, "recommended": True,
                                  "title": "Bacalhau com grão",
                                  "items": [{"food": "cod", "grams_low": 130,
                                             "grams_high": 160, "new": True}],
                                  "calories": 520, "protein_g": 42,
                                  "why": "Fecha a semana sem peixe."}]},
        "cards": [{"kind": "check_in", "title": "Titulo", "body": "Uma frase honesta.",
                   "chips": [{"label": "7 dias", "tone": "neutral"}]}],
    }

    def _queue_a_job(self):
        response = self.client.post("/coach/generate", json={"slot": "afternoon"},
                                    headers=self.auth)
        assert response.status_code == 202, response.get_json()
        return response.get_json()["job"]

    # -- the read path ---------------------------------------------------------

    def test_feed_is_empty_but_never_broken_before_anything_is_generated(self):
        """The screen the user opens must always render something. The old version
        showed a blank page here."""
        body = self.client.get("/coach/feed", headers=self.auth).get_json()
        assert body["status"] == "empty" and body["cards"] == []
        assert body["stale"] is True and body["generating"] is False

    def test_the_read_path_never_calls_a_model(self, monkeypatch):
        job = self._queue_a_job()
        self.client.post(f"/coach/work/{job}", json={"answer": self.ANSWER},
                         headers=self.auth)

        def explode(*_args, **_kwargs):
            raise AssertionError("the feed read must not call a model")
        monkeypatch.setattr(ingest._narrator_mod(), "call_gemini", explode)
        assert self.client.get("/coach/feed", headers=self.auth).status_code == 200

    # -- preparing work --------------------------------------------------------

    def test_generate_parks_a_job_instead_of_calling_a_model(self, monkeypatch):
        """Generation must not spend a model call here: the whole point is that the
        better model, on the Mac, gets first refusal."""
        def explode(*_args, **_kwargs):
            raise AssertionError("/coach/generate must not call Gemini")
        monkeypatch.setattr(ingest._narrator_mod(), "call_gemini", explode)
        body = self.client.post("/coach/generate", json={"slot": "afternoon"},
                                headers=self.auth).get_json()
        assert body["status"] == "queued" and body["waiting_for"] == "sonnet"

    def test_the_job_carries_a_prompt_full_of_food(self):
        job_id = self._queue_a_job()
        store_mod = ingest._coach("coach_store")
        job = store_mod.read_json(store_mod.job_path(job_id))
        # In pt-PT: the prompt is Portuguese and forbids naming a food that isn't in
        # it, so an English name here would be a food the coach cannot talk about.
        assert "arroz branco" in job["prompt"]
        assert "white rice" not in job["prompt"]
        assert "carne processada" in job["prompt"] or "processed_meat" in job["prompt"]
        assert job["context"]["findings"]

    def test_a_meal_still_being_eaten_postpones_the_analysis(self):
        """A second helping forty minutes later must not produce a second analysis —
        it should push the first one back."""
        store_mod = ingest._coach("coach_store")
        store_mod.update_state(lambda s: {**s,
                                          "last_meal_at": "2026-07-26T15:20:00"})
        body = self.client.post(
            "/coach/generate",
            json={"slot": "adhoc", "only_if_no_meal_since": "2026-07-26T14:30:00"},
            headers=self.auth).get_json()
        assert body["status"] == "superseded"

    def test_a_quiet_hour_lets_the_analysis_through(self):
        store_mod = ingest._coach("coach_store")
        store_mod.update_state(lambda s: {**s,
                                          "last_meal_at": "2026-07-26T14:00:00"})
        body = self.client.post(
            "/coach/generate",
            json={"slot": "adhoc", "only_if_no_meal_since": "2026-07-26T14:00:00"},
            headers=self.auth).get_json()
        assert body["status"] == "queued"

    # -- the Sonnet worker path ------------------------------------------------

    def test_the_worker_claims_a_job_and_its_answer_becomes_the_feed(self):
        job_id = self._queue_a_job()
        work = self.client.get("/coach/work?worker=mac", headers=self.auth).get_json()
        assert work["id"] == job_id and work["prompt"]

        applied = self.client.post(f"/coach/work/{job_id}",
                                   json={"answer": self.ANSWER, "model": "sonnet-5"},
                                   headers=self.auth).get_json()
        assert applied["status"] == "applied" and applied["cards"] >= 1

        cards = self.client.get("/coach/feed", headers=self.auth).get_json()["cards"]
        assert {"next_meal", "check_in"} <= {c["kind"] for c in cards}
        assert all(c["source"] == "sonnet-5" for c in cards)

    def test_an_empty_queue_answers_204(self):
        assert self.client.get("/coach/work", headers=self.auth).status_code == 204

    def test_two_workers_do_not_get_the_same_job(self):
        self._queue_a_job()
        first = self.client.get("/coach/work?worker=a", headers=self.auth)
        second = self.client.get("/coach/work?worker=b", headers=self.auth)
        assert first.status_code == 200 and second.status_code == 204

    def test_an_exhausted_usage_window_returns_the_job_to_the_queue(self):
        """This is the case that must NOT consume the job: it should keep waiting for
        Sonnet until the backend decides the wait has gone on long enough."""
        job_id = self._queue_a_job()
        self.client.get("/coach/work?worker=mac", headers=self.auth)
        released = self.client.post(f"/coach/work/{job_id}",
                                    json={"release": "usage limit reached"},
                                    headers=self.auth)
        assert released.get_json()["status"] == "released"
        again = self.client.get("/coach/work?worker=mac", headers=self.auth)
        assert again.status_code == 200 and again.get_json()["id"] == job_id

    def test_answering_an_unknown_job_is_a_404(self):
        assert self.client.post("/coach/work/nope", json={"answer": self.ANSWER},
                                headers=self.auth).status_code == 404

    def test_a_job_id_that_could_address_another_blob_is_rejected(self):
        assert self.client.post("/coach/work/..%2Fsecrets",
                                json={"answer": self.ANSWER},
                                headers=self.auth).status_code in (400, 404)

    # -- the Gemini fallback ---------------------------------------------------

    def test_the_sweep_leaves_a_young_job_for_sonnet(self):
        """Falling back early would defeat the point of preferring the better model."""
        job_id = self._queue_a_job()
        body = self.client.post("/coach/sweep", headers=self.auth).get_json()
        assert body["fell_back_to_gemini"] == []
        assert job_id in body["still_waiting_for_sonnet"]

    def test_the_sweep_takes_over_once_the_wait_is_too_long(self, monkeypatch):
        job_id = self._queue_a_job()
        store_mod = ingest._coach("coach_store")
        job = store_mod.read_json(store_mod.job_path(job_id))
        job["created_at"] = "2026-07-26T04:00:00"        # eleven hours earlier
        store_mod.write_json(store_mod.job_path(job_id), job)

        monkeypatch.setattr(ingest._narrator_mod(), "call_gemini",
                            lambda *a, **kw: self.ANSWER)
        body = self.client.post("/coach/sweep", headers=self.auth).get_json()
        assert body["fell_back_to_gemini"] == [job_id]

        cards = self.client.get("/coach/feed", headers=self.auth).get_json()["cards"]
        assert cards and all(c["source"] == "gemini" for c in cards)

    def test_both_models_pass_through_the_same_validation(self, monkeypatch):
        """Sonnet gets no more benefit of the doubt than Gemini: an invented swap is
        dropped whoever proposed it."""
        bad = {"cards": [{"kind": "check_in", "title": "T", "body": "B",
                          "swap": {"from": "pão branco", "to": "caviar",
                                   "why": "porque"}}]}
        job_id = self._queue_a_job()
        self.client.post(f"/coach/work/{job_id}", json={"answer": bad},
                         headers=self.auth)
        cards = self.client.get("/coach/feed", headers=self.auth).get_json()["cards"]
        assert cards and all(c["swap"] is None for c in cards)

    # -- progress reporting ----------------------------------------------------

    def test_a_job_waiting_for_a_sleeping_mac_is_not_a_spinner(self):
        """A job may legitimately wait hours for Sonnet. Showing a progress bar for
        that long is the old "loading forever" bug in a new costume."""
        self._queue_a_job()
        body = self.client.get("/coach/feed", headers=self.auth).get_json()
        assert body["generating"] is False and body["queued"] == 1

    def test_a_job_a_worker_is_running_is_a_spinner(self):
        self._queue_a_job()
        self.client.get("/coach/work?worker=mac", headers=self.auth)
        body = self.client.get("/coach/feed", headers=self.auth).get_json()
        assert body["generating"] is True

    def test_opening_the_app_in_a_new_part_of_the_day_asks_for_a_refresh(self,
                                                                        monkeypatch):
        job_id = self._queue_a_job()
        self.client.post(f"/coach/work/{job_id}", json={"answer": self.ANSWER},
                         headers=self.auth)
        assert self.client.get("/coach/feed",
                               headers=self.auth).get_json()["stale"] is False

        real_datetime = ingest.datetime.__mro__[1]

        class Evening(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 22, 0)
        monkeypatch.setattr(ingest, "datetime", Evening)
        body = self.client.get("/coach/feed", headers=self.auth).get_json()
        assert body["stale"] is True
        assert "check_in" not in {c["kind"] for c in body["cards"]}

    # -- the rest --------------------------------------------------------------

    def test_patterns_debug_view_is_deterministic_and_needs_no_model(self):
        profile = self.client.get("/coach/patterns", headers=self.auth).get_json()
        assert profile["days_logged"] == 7 and profile["findings"]

    # -- chat ------------------------------------------------------------------
    # Chat is queued work, like the feed: the question is recorded and parked, and
    # Sonnet answers it whenever the Mac is awake. See the banner above `coach_chat`
    # for the production bug that shape exists to make impossible.
    CHAT_ANSWER = {"reply": "Podes trocar por bacalhau.",
                   "memory_candidates": [{"type": "dislike",
                                          "fact": "não gosta de peixe cozido",
                                          "confidence": 0.9}]}

    def _a_card(self):
        job_id = self._queue_a_job()
        self.client.post(f"/coach/work/{job_id}", json={"answer": self.ANSWER},
                         headers=self.auth)
        return self.client.get("/coach/feed",
                               headers=self.auth).get_json()["cards"][0]

    def _ask(self, card, message="porque é que isto importa?", turn_id="turn-1"):
        body = {"thread_id": card["thread_id"], "card_id": card["id"],
                "message": message}
        if turn_id is not None:
            body["client_turn_id"] = turn_id
        return self.client.post("/coach/chat", json=body, headers=self.auth)

    def test_chat_queues_the_question_and_records_it_immediately(self):
        """The question must be in the transcript before any model has run — the app
        shows it straight away, and a lost answer must not lose the question too."""
        card = self._a_card()
        response = self._ask(card)
        assert response.status_code == 202
        body = response.get_json()
        assert body["status"] == "queued" and body["waiting_for"] == "sonnet"
        assert [t["role"] for t in body["turns"]] == ["user"]
        assert body["pending"] is True

    def test_the_chat_job_asks_for_sonnet_at_medium_effort(self):
        card = self._a_card()
        job_id = self._ask(card).get_json()["job"]
        store_mod = ingest._coach("coach_store")
        job = store_mod.read_json(store_mod.job_path(job_id))
        assert job["model"] == "claude-sonnet-5" and job["effort"] == "medium"
        assert job["require_key"] == "reply"
        # Without its own budget the worker reads "names a model" as "is a report".
        assert job["timeout_s"] == ingest.COACH_CHAT_TIMEOUT_S

    def test_chat_never_calls_a_model_in_the_request(self, monkeypatch):
        """The whole cause of the duplicates: a model in the request path made the
        request slow enough for the client to time out and retry."""
        def explode(*_args, **_kwargs):
            raise AssertionError("/coach/chat must not call a model")
        monkeypatch.setattr(ingest._narrator_mod(), "call_gemini", explode)
        monkeypatch.setattr(ingest._narrator_mod(), "chat_turn", explode)
        assert self._ask(self._a_card()).status_code == 202

    def test_the_answer_lands_in_the_thread_and_teaches_the_memory(self):
        card = self._a_card()
        job_id = self._ask(card).get_json()["job"]
        applied = self.client.post(f"/coach/work/{job_id}",
                                   json={"answer": self.CHAT_ANSWER},
                                   headers=self.auth).get_json()
        assert applied["chat"] == 1 and applied["memory_learned"] == 1

        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert [t["role"] for t in thread["turns"]] == ["user", "coach"]
        assert thread["turns"][1]["text"] == "Podes trocar por bacalhau."
        assert thread["pending"] is False       # nothing left in flight

        remembered = self.client.get("/coach/memory",
                                     headers=self.auth).get_json()["facts"]
        assert remembered[0]["fact"] == "não gosta de peixe cozido"

    def test_a_retried_send_does_not_ask_twice(self):
        """THE bug: the client retries every POST, so one tap produced three
        questions and three different answers, all saved to history."""
        card = self._a_card()
        first = self._ask(card, turn_id="turn-abc")
        again = self._ask(card, turn_id="turn-abc")
        third = self._ask(card, turn_id="turn-abc")

        assert first.status_code == 202
        # "already-asked" rather than "already-queued": the question is written to
        # the transcript before the job is created, so the transcript is what a
        # retry collides with. Both guards exist; this is the one that fires.
        assert again.get_json()["status"] == "already-asked"
        assert third.get_json()["status"] == "already-asked"

        store_mod = ingest._coach("coach_store")
        chat_jobs = [j for j in store_mod.list_jobs() if j.get("chat")]
        assert len(chat_jobs) == 1

        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert [t["role"] for t in thread["turns"]] == ["user"]

    def test_a_retry_after_the_answer_landed_is_still_not_a_second_question(self):
        """The retry can arrive late — after the worker already answered. The turn id
        is in the transcript by then, which is the check that catches it."""
        card = self._a_card()
        job_id = self._ask(card, turn_id="turn-xyz").get_json()["job"]
        self.client.post(f"/coach/work/{job_id}", json={"answer": self.CHAT_ANSWER},
                         headers=self.auth)

        late = self._ask(card, turn_id="turn-xyz")
        assert late.get_json()["status"] == "already-asked"
        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert [t["role"] for t in thread["turns"]] == ["user", "coach"]

    def test_two_genuinely_different_questions_both_get_through(self):
        """The guard must key on the id, not on the text — asking the same thing
        twice on purpose is allowed."""
        card = self._a_card()
        assert self._ask(card, turn_id="turn-1").status_code == 202
        second = self._ask(card, turn_id="turn-2")
        assert second.status_code == 202 and second.get_json()["status"] == "queued"
        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert [t["role"] for t in thread["turns"]] == ["user", "user"]

    def test_a_waiting_question_is_answered_before_a_scheduled_report(self):
        """Nobody is watching a weekly report arrive; someone is watching a chat."""
        card = self._a_card()
        self._queue_a_job()                      # a feed job, queued first
        chat_job = self._ask(card).get_json()["job"]
        store_mod = ingest._coach("coach_store")
        claimed = store_mod.claim_next_job("mac", "2026-07-26T16:00:00")
        assert claimed["id"] == chat_job

    def test_the_same_answer_applied_twice_appends_one_reply(self):
        """Sonnet finishing at the moment the sweeper hands the job to Gemini is the
        one way a turn can be answered twice."""
        card = self._a_card()
        job_id = self._ask(card).get_json()["job"]
        store_mod = ingest._coach("coach_store")
        job = store_mod.read_json(store_mod.job_path(job_id))
        now = datetime(2026, 7, 26, 16, 0)
        ingest._apply_chat(job, self.CHAT_ANSWER, now, source="sonnet")
        ingest._apply_chat(job, self.CHAT_ANSWER, now, source="gemini")

        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert [t["role"] for t in thread["turns"]] == ["user", "coach"]

    def test_chat_validates_before_it_reads_the_environment(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert self.client.post("/coach/chat", json={"thread_id": "t-1",
                                                     "message": ""},
                                headers=self.auth).status_code == 400
        assert self.client.post("/coach/chat", json={"thread_id": "../etc",
                                                     "message": "olá"},
                                headers=self.auth).status_code == 400

    def test_generate_rejects_an_unknown_slot(self):
        assert self.client.post("/coach/generate", json={"slot": "teatime"},
                                headers=self.auth).status_code == 400

    @pytest.mark.parametrize("path,method", [
        ("/coach/feed", "get"), ("/coach/patterns", "get"),
        ("/coach/memory", "get"), ("/coach/refresh", "post"),
        ("/coach/generate", "post"), ("/coach/chat", "post"),
        ("/coach/work", "get"), ("/coach/sweep", "post"),
    ])
    def test_every_coach_route_requires_the_token(self, path, method):
        assert getattr(self.client, method)(path).status_code == 401

    def test_refresh_answers_immediately_even_with_no_queue(self):
        """The app must never be left waiting on infrastructure it can't see."""
        response = self.client.post("/coach/refresh", json={"reason": "manual"},
                                    headers=self.auth)
        assert response.status_code == 202
        assert response.get_json()["queued"] is False


# -- quota handling ------------------------------------------------------------
# The coach shares a free-tier allowance with the meal-analysis pipeline, so a 429 is
# routine. It must read as "ask again shortly", never as an empty feed.

import narrator                                                  # noqa: E402


class TestQuota:
    def test_the_wait_comes_from_the_api_not_a_guess(self):
        detail = ('{"error": {"code": 429, "message": "Quota exceeded ... '
                  'Please retry in 26.929601479s."}}')
        assert narrator._retry_delay_s(detail, 0) == pytest.approx(27.93, abs=0.01)

    def test_the_wait_is_capped(self):
        assert narrator._retry_delay_s("Please retry in 600s.", 0) == \
            narrator.QUOTA_MAX_WAIT_S

    def test_a_message_without_a_hint_backs_off(self):
        first = narrator._retry_delay_s("no hint here", 0)
        second = narrator._retry_delay_s("no hint here", 1)
        assert second > first

    def test_a_quota_error_is_distinguishable_from_a_real_failure(self):
        """`coach_generate` turns this one into a 503 so the queue retries, and every
        other failure into a quiet, non-retried degradation."""
        assert issubclass(narrator.GeminiQuotaError, narrator.GeminiError)


class TestClipping:
    """A live day summary once ended "Amanhã, o pequeno-almoço e" — the model wrote a
    good closing thought and the assembly ate it."""

    def test_short_text_is_untouched(self):
        assert feed._clip("Uma frase curta.", 100) == "Uma frase curta."

    def test_whitespace_is_collapsed(self):
        assert feed._clip("duas   linhas\ne  espaços", 100) == "duas linhas e espaços"

    def test_a_long_body_is_cut_at_a_sentence_end(self):
        text = ("Primeira frase completa. Segunda frase completa. "
                "Terceira frase que vai ser cortada a meio")
        out = feed._clip(text, 55)
        assert out.endswith(".")
        assert "cortada" not in out

    def test_text_with_no_sentence_break_is_cut_at_a_word(self):
        out = feed._clip("palavra " * 40, 50)
        assert out.endswith("…") and not out.endswith("palav…")

    def test_the_day_card_has_room_for_a_real_reading(self):
        profile = profile_from(a_week_of_meals())
        body = ("Frase. " * 150).strip()
        cards, _ = generate("evening", profile, an_answer(
            [{"kind": "day_summary", "title": "T", "body": body}], plates=False))
        assert len(cards[0]["body"]) > 700


# =============================================================================
# Memory: the archive, event detection, and budgeted recall.
#
# The research this design follows (MemTier, 2026) measured that retrieval — not model
# size — is the binding constraint on agent memory, with multi-session recall@2 at
# 0.038. These tests pin the retrieval, because that is where the quality lives.
# =============================================================================

import coach_archive as archive      # noqa: E402
import coach_events as events        # noqa: E402
import coach_recall as recall        # noqa: E402
import coach_reports as reports      # noqa: E402


def a_meal(day, time, foods, calories=500, protein=30, note=""):
    return {"datetime": f"{day}T{time}:00", "date": day,
            "slot": patterns.meal_slot(f"{day}T{time}:00"),
            "calories": calories, "protein_g": protein, "note": note,
            "items": [{"raw": f, "food": tax.canonical_name(f),
                       "group": tax.lookup(None, f)["group"],
                       "fried": tax.is_fried(f), "grams": 150, "calories": 200,
                       "servings": tax.servings(tax.lookup(None, f)["group"], 150),
                       "nutrients": {}} for f in foods],
            "groups": sorted({tax.lookup(None, f)["group"] for f in foods})}


class TestEventDetection:
    """`food_patterns` answers "how does this person eat". This answers "what
    happened on Friday", which averages destroy."""

    def test_a_night_out_is_one_event_not_eight_drinks(self):
        night = [a_meal("2026-07-24", "22:10", ["beer", "beer"]),
                 a_meal("2026-07-24", "23:40", ["vodka", "vodka", "beer"]),
                 a_meal("2026-07-25", "01:30", ["big tasty burger"])]
        found = events.detect(night, day="2026-07-24")
        drinking = [e for e in found if e["kind"] == "drinking_occasion"]
        assert drinking, [e["kind"] for e in found]
        assert drinking[0]["importance"] >= 0.85
        assert "sexta-feira" in drinking[0]["headline"]
        assert drinking[0]["evidence"]["first"] == "22:10"

    def test_one_glass_of_wine_is_not_an_occasion(self):
        found = events.detect([a_meal("2026-07-26", "13:30", ["white wine"])],
                              day="2026-07-26")
        drinking = [e for e in found if e["kind"].startswith("drinking")]
        assert drinking and drinking[0]["kind"] == "drinking"
        assert drinking[0]["importance"] <= 0.4

    def test_the_restaurant_is_found_in_the_note_not_the_items(self):
        """The items say "burger, fries, iced tea". Only the note says McDonald's."""
        meals = [a_meal("2026-07-26", "20:00",
                        ["big tasty burger", "french fries", "iced tea"],
                        calories=1250)]
        notes = {"2026-07-26T20:00:00":
                 "Comi um menu médio Big Tasty do McDonalds com os amigos"}
        found = events.detect(meals, day="2026-07-26", notes=notes)
        out = [e for e in found if e["kind"] == "eaten_out"]
        assert out and "mcdonald" in out[0]["evidence"]["markers"]
        assert "McDonalds" in out[0]["detail"]

    def test_a_user_who_says_they_overdid_it_is_heard(self):
        """Someone telling you they already know changes what is worth saying."""
        meals = [a_meal("2026-07-26", "20:00", ["french fries"])]
        notes = {"2026-07-26T20:00:00": "Exagerei hoje, sei que não devia"}
        noted = [e for e in events.detect(meals, day="2026-07-26", notes=notes)
                 if e["kind"] == "noted"]
        assert noted and noted[0]["evidence"]["self_aware"] is True
        assert noted[0]["importance"] >= 0.7

    def test_a_day_far_over_the_ceiling_is_an_event(self):
        found = events.detect([a_meal("2026-07-26", "20:00", ["french fries"])],
                              day="2026-07-26", calories=3200, calorie_ceiling=2200)
        assert any(e["kind"] == "big_day" for e in found)

    def test_an_ordinary_day_produces_nothing(self):
        """Silence is the correct output most days; an event log that fires daily is
        just a second copy of the meal log."""
        ordinary = [a_meal("2026-07-26", "08:15", ["oats", "peanut butter"]),
                    a_meal("2026-07-26", "13:00", ["white rice", "boiled cod"])]
        assert events.detect(ordinary, day="2026-07-26",
                             calories=1900, calorie_ceiling=2200) == []

    def test_the_day_is_ranked_by_its_most_notable_moment(self):
        night = events.detect(
            [a_meal("2026-07-24", "22:10", ["beer", "beer", "vodka", "beer"])],
            day="2026-07-24")
        assert events.day_importance(night) >= 0.85
        assert events.day_importance([]) < 0.2


class TestArchive:
    @pytest.fixture(autouse=True)
    def _local(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COACH_BUCKET", raising=False)
        monkeypatch.setenv("COACH_LOCAL_DIR", str(tmp_path))

    def _card(self, day, kind="pattern", title="T", topic="red_meat"):
        return {"id": f"{day}:{kind}:{topic}", "kind": kind, "date": day,
                "created_at": f"{day}T15:30:00", "title": title, "body": "Corpo.",
                "topic": topic, "priority": 70, "source": "claude-sonnet-5",
                "evidence": {"finding": f"group_over:{topic}"},
                "swap": {"from": "beef steak", "to": "cod"}}

    def test_what_the_feed_forgets_the_archive_keeps(self):
        archive.record_cards([self._card("2026-07-20")], now=NOW)
        kept = archive.read_range("2026-07-01", "2026-07-31", kinds=("card",))
        assert len(kept) == 1 and kept[0]["summary"] == "T"

    def test_appending_the_same_card_twice_does_not_duplicate_it(self):
        """A Cloud Tasks retry re-posts the same cards; the archive must not grow."""
        for _ in range(3):
            archive.record_cards([self._card("2026-07-20")], now=NOW)
        assert len(archive.read_range("2026-07-01", "2026-07-31")) == 1

    def test_a_card_carries_the_keys_it_will_be_found_by(self):
        archive.record_cards([self._card("2026-07-20")], now=NOW)
        topics = archive.read_range("2026-07-01", "2026-07-31")[0]["topics"]
        assert "red_meat" in topics
        assert "finding:group_over:red_meat" in topics
        assert "swap_from:beef steak" in topics

    def test_entries_are_sharded_by_month_and_read_across_them(self):
        archive.record_cards([self._card("2026-06-28"), self._card("2026-07-02")],
                             now=NOW)
        assert len(archive.read_range("2026-06-01", "2026-07-31")) == 2
        assert len(archive.read_range("2026-07-01", "2026-07-31")) == 1

    def test_conversations_are_kept(self):
        archive.record_chat("t-1", day="2026-07-20", at="21:04",
                            question="não gosto de peixe cozido",
                            answer="Então vamos por assado.")
        chats = archive.read_range("2026-07-01", "2026-07-31", kinds=("chat",))
        assert chats and chats[0]["data"]["answer"].startswith("Então")

    def test_reports_are_stored_and_listed_newest_first(self):
        for key in ("2026-07-06", "2026-07-13", "2026-07-20"):
            archive.save_report("weekly", key, {"period": "weekly", "key": key,
                                                "headline": f"semana {key}"})
        listed = archive.recent_reports("weekly", before="9999", limit=2)
        assert [r["key"] for r in listed] == ["2026-07-20", "2026-07-13"]

    def test_a_report_can_read_only_what_came_before_it(self):
        """A monthly must not read a weekly from the future when it is re-run."""
        for key in ("2026-07-06", "2026-07-13"):
            archive.save_report("weekly", key, {"key": key})
        assert [r["key"] for r in
                archive.recent_reports("weekly", before="2026-07-13")] == ["2026-07-06"]

    def test_stats_report_what_is_held(self):
        archive.record_cards([self._card("2026-07-20")], now=NOW)
        archive.record_chat("t-1", day="2026-07-20", at="10:00", question="q",
                            answer="a")
        held = archive.stats()
        assert held["total"] == 2 and held["by_kind"]["card"] == 1


class TestRecall:
    """Ranking is where a memory system is won or lost: the point is not to remember
    everything, it is to remember the right thing now."""

    def _entry(self, day, topics, summary="x", importance=0.5, kind="card"):
        return archive.entry(kind, day=day, at="12:00", id=f"{kind}:{day}:{summary}",
                             summary=summary, topics=topics, importance=importance)

    def test_an_on_topic_memory_beats_a_recent_irrelevant_one(self):
        old_relevant = self._entry("2026-06-01", ["alcohol", "beer"], "a noite")
        new_irrelevant = self._entry("2026-07-25", ["oats"], "aveia")
        ranked = recall.rank([new_irrelevant, old_relevant], today="2026-07-26",
                             query_topics=["alcohol"])
        assert [e["summary"] for e in ranked] == ["a noite"]

    def test_nothing_off_topic_is_recalled_at_all(self):
        """Without this floor, recency alone drags in whatever happened to be recent —
        which is how a memory system ends up padding prompts with noise."""
        assert recall.rank([self._entry("2026-07-25", ["oats"])],
                           today="2026-07-26", query_topics=["alcohol"]) == []

    def test_recency_breaks_ties_between_equally_relevant_memories(self):
        older = self._entry("2026-05-01", ["alcohol"], "maio")
        newer = self._entry("2026-07-20", ["alcohol"], "julho")
        ranked = recall.rank([older, newer], today="2026-07-26",
                             query_topics=["alcohol"])
        assert [e["summary"] for e in ranked] == ["julho", "maio"]

    def test_importance_lifts_a_memorable_night_over_a_routine_note(self):
        night = self._entry("2026-07-10", ["alcohol"], "festa", importance=0.9)
        routine = self._entry("2026-07-10", ["alcohol"], "um copo", importance=0.2)
        ranked = recall.rank([routine, night], today="2026-07-26",
                             query_topics=["alcohol"])
        assert ranked[0]["summary"] == "festa"

    def test_recency_decays_by_half_at_the_half_life(self):
        assert recall.recency_score("2026-07-26", "2026-07-26") == pytest.approx(1.0)
        assert recall.recency_score("2026-07-12", "2026-07-26") == pytest.approx(0.5)

    def test_partial_topic_matches_still_find_the_memory(self):
        entry = self._entry("2026-07-20", ["swap_from:red_meat"])
        assert recall.relevance_score(entry["topics"], ["red_meat"]) > 0

    def test_the_query_is_built_from_what_actually_happened_today(self):
        topics = recall.query_topics_for(
            profile={}, today={"meals": [{"food_groups": ["alcohol"],
                                          "items": [{"food": "beer"}]}]},
            events=[{"kind": "drinking_occasion", "topics": ["alcohol"]}],
            findings=[{"id": "group_over:red_meat", "group": "red_meat"}])
        assert "alcohol" in topics and "kind:drinking_occasion" in topics
        assert "finding:group_over:red_meat" in topics

    def test_a_quiet_day_asks_for_almost_nothing(self):
        """Retrieval cost should scale with how interesting the day was."""
        topics = recall.query_topics_for(profile={}, today={"meals": []}, events=[])
        assert topics == []


class TestMemoryBudget:
    """"We cannot inject a massive context history into every prompt" — made
    mechanical, not aspirational."""

    def _entries(self, count, topic="alcohol"):
        return [archive.entry("card", day="2026-07-20", at="12:00", id=f"c{i}",
                              summary=f"memoria numero {i} " + "detalhe " * 30,
                              topics=[topic], importance=0.5)
                for i in range(count)]

    def test_sections_stay_inside_their_budgets(self):
        memory = recall.assemble(
            today_iso="2026-07-26",
            profile_facts=[{"type": "dislike", "fact": "x " * 200}] * 20,
            recent_cards=self._entries(30), archive=self._entries(60),
            events=[{"headline": "h " * 100, "at": "20:00"}] * 20,
            reports=[{"period": "weekly", "key": "2026-07-20",
                      "summary": "s " * 500}] * 10,
            query_topics=["alcohol"])
        for section, budget in recall.BUDGET.items():
            assert memory["_tokens"][section] <= budget * 1.15, section

    def test_the_whole_memory_block_stays_bounded(self):
        memory = recall.assemble(
            today_iso="2026-07-26", profile_facts=self._entries(50),
            recent_cards=self._entries(50), archive=self._entries(200),
            events=[], reports=[], query_topics=["alcohol"])
        assert memory["_tokens"]["total"] <= sum(recall.BUDGET.values()) * 1.15

    def test_items_are_dropped_whole_rather_than_truncated(self):
        """Half a memory is worse than none: the model cannot tell which half is
        missing."""
        memory = recall.assemble(
            today_iso="2026-07-26", profile_facts=[], recent_cards=[],
            archive=self._entries(60), events=[], reports=[],
            query_topics=["alcohol"])
        assert all(m["what"].startswith("memoria numero")
                   for m in memory["you_might_recall"])

    def test_an_empty_history_costs_nothing(self):
        memory = recall.assemble(
            today_iso="2026-07-26", profile_facts=[], recent_cards=[], archive=[],
            events=[], reports=[], query_topics=[])
        assert memory["_tokens"]["total"] < 60


class TestReportPeriods:
    def test_a_weekly_covers_the_week_that_just_ended(self):
        # Monday 2026-07-27 -> the week Mon 20th to Sun 26th.
        start, end, key = reports.period_bounds("weekly", date(2026, 7, 27))
        assert (start, end, key) == ("2026-07-20", "2026-07-26", "2026-07-20")

    def test_a_report_never_covers_a_period_still_running(self):
        """A review of a week that hasn't finished would be revised by Sunday
        dinner."""
        _start, end, _key = reports.period_bounds("weekly", date(2026, 7, 23))
        assert end < "2026-07-23"

    def test_monthly_and_yearly_bounds(self):
        assert reports.period_bounds("monthly", date(2026, 7, 15)) == (
            "2026-06-01", "2026-06-30", "2026-06")
        assert reports.period_bounds("yearly", date(2026, 3, 2)) == (
            "2025-01-01", "2025-12-31", "2025")

    def test_a_weekly_reads_the_week_whole(self):
        meals = [a_meal("2026-07-20", "13:00", ["white rice", "beef steak"],
                        note="almoço rápido")]
        facts = reports.weekly_facts(
            start="2026-07-20", end="2026-07-26", key="2026-07-20", meals=meals,
            profile=profile_from(a_week_of_meals()),
            archive_entries=[
                archive.entry("card", day="2026-07-21", at="08:00", id="c1",
                              summary="Menos carne", topics=["red_meat"]),
                archive.entry("chat", day="2026-07-22", at="21:00", id="h1",
                              summary="porquê?", topics=["chat"],
                              data={"question": "porquê?", "answer": "porque sim"})])
        assert facts["meals"][0]["note"] == "almoço rápido"
        assert facts["what_you_were_told"][0]["title"] == "Menos carne"
        assert facts["conversations"][0]["you_asked"] == "porquê?"

    def test_a_rollup_reads_only_the_level_below(self):
        """This is what keeps a yearly report affordable: twelve summaries, not a year
        of meals."""
        facts = reports.rollup_facts(
            period="monthly", start="2026-06-01", end="2026-06-30", key="2026-06",
            children=[{"key": "2026-06-01", "headline": "h", "summary": "s"}])
        assert "meals" not in facts and facts["made_of"][0]["headline"] == "h"

    def test_a_report_is_validated_into_shape(self):
        report = reports.assemble_report(
            {"headline": "x " * 300, "summary": "s", "wins": [{"title": "w"}] * 20,
             "focus": {"label": "l", "why": "y", "how": "h"},
             "meal_reviews": [{"what": "almoço", "verdict": "good", "why": "w"}]},
            period="weekly", key="2026-07-20", start="2026-07-20", end="2026-07-26",
            now=NOW, source="claude-opus-5")
        assert len(report["headline"]) <= 200
        assert len(report["wins"]) <= 5
        assert report["focus"]["label"] == "l"
        assert report["meal_reviews"][0]["verdict"] == "good"

    def test_the_report_prompt_asks_whether_the_advice_landed(self):
        prompt = reports.build_prompt({"period": "weekly", "covering": {}})
        assert "what_you_were_told" in prompt and "o conselho pegou" in prompt

    def test_a_report_becomes_a_card(self):
        card = reports.as_card_fields(
            {"period": "weekly", "key": "2026-07-20", "headline": "A semana",
             "summary": "Correu assim.", "focus": {"label": "peixe 2x"}})
        assert card["kind"] == "weekly_review"
        assert "peixe 2x" in card["body"]
