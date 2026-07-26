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
from datetime import datetime, timedelta

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


def fake_narrate(cards):
    def narrate(_facts):
        return {"cards": cards}
    return narrate


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

        def run(slot):
            cards, _ = feed.generate_cards(
                slot=slot, now=NOW, profile=profile,
                today={"meals": [], "calories_left": 900}, nutrients={}, memory={},
                state={}, narrate=fake_narrate([]),
                plates_fn=lambda: {"next_slot": "jantar", "reasoning": "porque",
                                   "plates": [{"rank": 1, "title": "Prato"}]})
            return cards

        merged = feed.merge_cards(run("afternoon"), run("adhoc"), now=NOW)
        assert len([c for c in merged if c["kind"] == "next_meal"]) == 1

    def test_a_rerun_does_not_stack_duplicates(self):
        profile = profile_from(a_week_of_meals())
        args = dict(slot="afternoon", now=NOW, profile=profile,
                    today={"meals": [], "calories_left": 900},
                    nutrients={}, memory={}, state={},
                    narrate=fake_narrate([{"kind": "check_in", "title": "Olá",
                                           "body": "Corpo do cartão."}]))
        one, _ = feed.generate_cards(**args)
        two, _ = feed.generate_cards(**args)
        merged = feed.merge_cards(one, two, now=NOW)
        assert len(merged) == 1

    def test_expired_cards_drop_out_of_the_feed(self):
        stale = {"id": "old", "priority": 90, "created_at": "2026-07-20T08:00:00",
                 "expires_at": "2026-07-21T08:00:00"}
        live = {"id": "new", "priority": 50, "created_at": "2026-07-26T08:00:00",
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
        cards, _ = feed.generate_cards(
            slot="afternoon", now=NOW, profile=profile, today={"meals": []},
            nutrients={}, memory={}, state={},
            narrate=fake_narrate([{"kind": "check_in", "title": "T",
                                   "body": "B"}]))
        assert cards[0]["thread_id"]
        assert cards[0]["thread_id"] == feed.thread_id_for(cards[0])
        assert store.is_safe_id(cards[0]["thread_id"])


class TestSwapValidation:
    """The guard that replaces a prompt rule the model demonstrably broke in
    production: it proposed swapping "pão branco" for a user who had never logged
    any bread."""

    def setup_method(self):
        self.profile = profile_from(a_week_of_meals())
        # Whatever the feed would actually put on a card today: only an eligible
        # finding gets one, so only its swap options are on offer.
        self.finding = feed.eligible_findings(self.profile, {},
                                              today="2026-07-26")[0]
        self.offered = self.profile["swaps"][self.finding["id"]]["to"][0]["food"]

    def _run(self, swap):
        cards, _ = feed.generate_cards(
            slot="afternoon", now=NOW, profile=self.profile,
            today={"meals": []}, nutrients={}, memory={},
            state={}, narrate=fake_narrate([
                {"kind": "pattern", "ref": self.finding["id"], "title": "T",
                 "body": "B", "swap": swap}]))
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
        assert swap and swap["from"] == "beef steak" and swap["to"] == self.offered
        assert isinstance(swap["new"], bool)

    def test_a_pattern_card_without_a_real_finding_is_dropped(self):
        cards, shown = feed.generate_cards(
            slot="afternoon", now=NOW, profile=self.profile, today={"meals": []},
            nutrients={}, memory={}, state={},
            narrate=fake_narrate([{"kind": "pattern", "ref": "made:up",
                                   "title": "T", "body": "B"}]))
        assert cards == [] and shown == {}


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
        top = profile["findings"][0]
        _cards, shown = feed.generate_cards(
            slot="morning", now=NOW, profile=profile, today={"meals": []},
            nutrients={}, memory={}, state={},
            narrate=fake_narrate([{"kind": "pattern", "ref": top["id"],
                                   "title": "T", "body": "B"}]))
        assert shown[top["id"]]["date"] == "2026-07-26"


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

    def test_a_plate_failure_does_not_lose_the_other_cards(self):
        def boom():
            raise RuntimeError("gemini down")
        cards, _ = feed.generate_cards(
            slot="morning", now=NOW, profile=profile_from(a_week_of_meals()),
            today={"meals": []}, nutrients={}, memory={}, state={},
            narrate=fake_narrate([{"kind": "day_plan", "title": "T", "body": "B"}]),
            plates_fn=boom)
        assert [c["kind"] for c in cards] == ["day_plan"]

    def test_a_narration_failure_does_not_lose_the_plates(self):
        def narrate(_facts):
            raise RuntimeError("gemini down")
        cards, _ = feed.generate_cards(
            slot="morning", now=NOW, profile=profile_from(a_week_of_meals()),
            today={"meals": [], "calories_left": 800}, nutrients={}, memory={},
            state={}, narrate=narrate,
            plates_fn=lambda: {"next_slot": "jantar", "reasoning": "porque",
                               "plates": [{"rank": 1, "title": "Prato"}]})
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
        # Freeze "now" inside the logged window so the fixture reads as recent.
        monkeypatch.setattr(ingest, "_tz", lambda: None)
        real_datetime = ingest.datetime

        class FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 15, 30)
        monkeypatch.setattr(ingest, "datetime", FrozenDatetime)
        self.client = ingest.app.test_client()
        self.auth = {"X-Auth-Token": "tok"}

    def _fake_model(self, monkeypatch, cards=None, plates=True):
        narrator = ingest._narrator_mod()
        monkeypatch.setattr(narrator, "narrate_cards", lambda facts, **kw: {
            "cards": cards if cards is not None else [
                {"kind": k, "title": f"Titulo {k}", "body": "Uma frase honesta.",
                 "chips": [{"label": "7 dias", "tone": "neutral"}]}
                for k in ("check_in",)]})
        monkeypatch.setattr(narrator, "narrate_next_meal", lambda ctx, **kw: {
            "next_slot": "jantar", "reasoning": "Porque já almoçaste.",
            "plates": [{"rank": 1, "recommended": True, "title": "Bacalhau com grão",
                        "items": [{"food": "bacalhau", "grams_low": 130,
                                   "grams_high": 160, "new": True}],
                        "calories": 520, "protein_g": 42,
                        "why": "Fecha a semana sem peixe."}] if plates else []})
        # Taxonomy learning must never reach the network in a test.
        monkeypatch.setattr(narrator, "call_gemini",
                            lambda *a, **kw: {"foods": []})

    def test_feed_is_empty_but_never_broken_before_anything_is_generated(self):
        """The screen the user opens must always render something. The old version
        showed a blank page here."""
        response = self.client.get("/coach/feed", headers=self.auth)
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "empty" and body["cards"] == []
        assert body["stale"] is True and body["generating"] is False

    def test_generate_then_read(self, monkeypatch):
        self._fake_model(monkeypatch)
        gen = self.client.post("/coach/generate", json={"slot": "afternoon"},
                               headers=self.auth)
        assert gen.status_code == 200, gen.get_json()
        assert gen.get_json()["status"] == "generated"

        feed_response = self.client.get("/coach/feed", headers=self.auth)
        body = feed_response.get_json()
        kinds = {c["kind"] for c in body["cards"]}
        assert {"next_meal", "check_in"} <= kinds
        assert body["status"] == "ready" and body["stale"] is False
        # Plates rode along on the card, so opening the suggestion needs no call.
        plates = next(c for c in body["cards"] if c["kind"] == "next_meal")["plates"]
        assert plates and plates[0]["title"] == "Bacalhau com grão"

    def test_the_read_path_never_calls_a_model(self, monkeypatch):
        self._fake_model(monkeypatch)
        self.client.post("/coach/generate", json={"slot": "afternoon"},
                         headers=self.auth)

        def explode(*_args, **_kwargs):
            raise AssertionError("the feed read must not call a model")
        narrator = ingest._narrator_mod()
        monkeypatch.setattr(narrator, "call_gemini", explode)
        monkeypatch.setattr(narrator, "narrate_cards", explode)
        assert self.client.get("/coach/feed", headers=self.auth).status_code == 200

    def test_regenerating_replaces_rather_than_stacks(self, monkeypatch):
        self._fake_model(monkeypatch)
        for _ in range(3):
            ingest._coach("coach_store").update_state(
                lambda s: {**s, "job": None})       # release the single-run lock
            self.client.post("/coach/generate", json={"slot": "afternoon"},
                             headers=self.auth)
        cards = self.client.get("/coach/feed", headers=self.auth).get_json()["cards"]
        assert len(cards) == len({c["id"] for c in cards})
        assert len([c for c in cards if c["kind"] == "next_meal"]) == 1

    def test_a_second_concurrent_generation_is_refused(self, monkeypatch):
        self._fake_model(monkeypatch)
        store_mod = ingest._coach("coach_store")
        store_mod.claim_job("other", "manual", "2026-07-26T15:29:50")
        response = self.client.post("/coach/generate", json={"slot": "afternoon"},
                                    headers=self.auth)
        assert response.get_json()["status"] == "busy"

    def test_generation_survives_a_model_that_returns_nothing_useful(self,
                                                                    monkeypatch):
        """A bad model response must leave the feature quiet, not wedged."""
        self._fake_model(monkeypatch, cards=[{"kind": "check_in", "title": "",
                                              "body": ""}], plates=False)
        response = self.client.post("/coach/generate", json={"slot": "afternoon"},
                                    headers=self.auth)
        assert response.status_code == 200
        assert response.get_json()["status"] == "empty"
        assert self.client.get("/coach/feed",
                               headers=self.auth).get_json()["generating"] is False

    def test_patterns_debug_view_is_deterministic_and_needs_no_model(self):
        response = self.client.get("/coach/patterns", headers=self.auth)
        assert response.status_code == 200
        profile = response.get_json()
        assert profile["days_logged"] == 7
        assert profile["findings"]

    def test_chat_appends_turns_and_remembers(self, monkeypatch):
        self._fake_model(monkeypatch)
        self.client.post("/coach/generate", json={"slot": "afternoon"},
                         headers=self.auth)
        card = self.client.get("/coach/feed",
                               headers=self.auth).get_json()["cards"][0]
        narrator = ingest._narrator_mod()
        monkeypatch.setattr(narrator, "chat_turn", lambda *a, **kw: {
            "reply": "Podes trocar por bacalhau.",
            "memory_candidates": [{"type": "dislike",
                                   "fact": "não gosta de peixe cozido",
                                   "confidence": 0.9}]})
        response = self.client.post("/coach/chat", json={
            "thread_id": card["thread_id"], "card_id": card["id"],
            "message": "porque é que isto importa?"}, headers=self.auth)
        assert response.status_code == 200
        body = response.get_json()
        assert body["reply"] == "Podes trocar por bacalhau."
        assert [t["role"] for t in body["turns"]] == ["user", "coach"]
        assert body["memory_learned"] == 1

        thread = self.client.get(f"/coach/thread/{card['thread_id']}",
                                 headers=self.auth).get_json()
        assert len(thread["turns"]) == 2
        remembered = self.client.get("/coach/memory",
                                     headers=self.auth).get_json()["facts"]
        assert remembered[0]["fact"] == "não gosta de peixe cozido"

    def test_chat_validates_before_it_reads_the_environment(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert self.client.post("/coach/chat", json={"thread_id": "t-1",
                                                     "message": ""},
                                headers=self.auth).status_code == 400
        assert self.client.post("/coach/chat", json={"thread_id": "../etc",
                                                     "message": "olá"},
                                headers=self.auth).status_code == 400

    def test_generate_rejects_an_unknown_slot(self):
        response = self.client.post("/coach/generate", json={"slot": "teatime"},
                                    headers=self.auth)
        assert response.status_code == 400

    @pytest.mark.parametrize("path,method", [
        ("/coach/feed", "get"), ("/coach/patterns", "get"),
        ("/coach/memory", "get"), ("/coach/refresh", "post"),
        ("/coach/generate", "post"), ("/coach/chat", "post"),
    ])
    def test_every_coach_route_requires_the_token(self, path, method):
        assert getattr(self.client, method)(path).status_code == 401

    def test_refresh_answers_immediately_even_with_no_queue(self):
        """The app must never be left waiting on infrastructure it can't see."""
        response = self.client.post("/coach/refresh", json={"reason": "manual"},
                                    headers=self.auth)
        assert response.status_code == 202
        assert response.get_json()["queued"] is False

    def test_a_long_lived_card_survives_later_generations(self):
        """The Sunday review is valid for eight days. Per-day feed blobs quietly broke
        this: by Wednesday it was outside the window the reader looked at."""
        store_mod = ingest._coach("coach_store")
        feed_mod = ingest._coach("coach_feed")
        sunday = ingest.datetime(2026, 7, 26, 9, 0)
        weekly = feed_mod._card(kind="weekly_review", slot="weekly",
                                date="2026-07-26", now=sunday,
                                title="A tua semana", body="Corpo.")
        store_mod.write_json(store_mod.FEED, {"cards": [weekly]})

        # Three days later, a routine afternoon run lands.
        later = ingest.datetime(2026, 7, 29, 15, 30)
        fresh = feed_mod._card(kind="check_in", slot="afternoon",
                               date="2026-07-29", now=later, title="Hoje",
                               body="Corpo.")
        merged = feed_mod.merge_cards([weekly], [fresh], now=later)
        assert {c["kind"] for c in merged} == {"weekly_review", "check_in"}


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
