"""The non-food half of the coach: per-domain findings and the link engine.

Two things these exist to protect. First, that a finding never fires on thin or
noisy data — a coach that invents urgency from three days is one you stop trusting.
Second, and more subtly, that a link is measured with the RIGHT DAYS PAIRED: food on
row N meets the night recorded on row N+1, and getting that backwards would ask
whether tomorrow's dinner affected last night's sleep while still producing
confident, plausible cards.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))

domain_findings = importlib.import_module("domain_findings")
links = importlib.import_module("links")

from schema.registry import BY_NAME, CAUSAL_INPUT, CAUSAL_OUTCOME


def days_from(start_day=1, **series):
    """Rows of daily_summary from parallel lists, dated consecutively."""
    length = max(len(v) for v in series.values())
    out = []
    for i in range(length):
        row = {"date": f"2026-06-{start_day + i:02d}"}
        for name, values in series.items():
            if i < len(values) and values[i] is not None:
                row[name] = values[i]
        out.append(row)
    return out


class TestSilenceOnThinData:
    def test_nothing_fires_below_the_minimum_days(self):
        # Five catastrophic nights are still only five nights.
        days = days_from(sleep_mins=[300] * 5)
        assert domain_findings.build_findings(days) == []

    def test_no_domains_means_no_findings(self):
        # The capability gate, and the only place it needs to appear.
        days = days_from(sleep_mins=[300] * 30)
        assert domain_findings.build_findings(days, domains=()) == []

    def test_a_disabled_domain_is_silent_while_others_speak(self):
        days = days_from(sleep_mins=[300] * 30, steps=[9000] * 30)
        found = domain_findings.build_findings(days, domains=("activity",))
        assert all(f["domain"] == "activity" for f in found)

    def test_missing_values_are_skipped_not_zeroed(self):
        # A night the tracker wasn't worn is missing data. Counting it as zero
        # minutes of sleep would manufacture a crisis out of a flat battery.
        days = days_from(sleep_mins=[460] * 15 + [None] * 10)
        found = domain_findings.build_findings(days, domains=("sleep",))
        assert [f for f in found if f["kind"] == "short_sleep"] == []


class TestSleepFindings:
    def test_persistently_short_sleep_is_a_finding(self):
        days = days_from(sleep_mins=[470] * 14 + [360, 355, 370, 350, 365, 358, 362])
        found = domain_findings.build_findings(days, domains=("sleep",))
        kinds = {f["kind"] for f in found}
        assert "short_sleep" in kinds
        hit = next(f for f in found if f["kind"] == "short_sleep")
        assert hit["domain"] == "sleep"
        assert hit["evidence"]["days_bad"] >= 3
        assert 0 < hit["severity"] <= 1

    def test_one_bad_night_in_a_good_run_is_not_a_pattern(self):
        days = days_from(sleep_mins=[470] * 20 + [330])
        found = domain_findings.build_findings(days, domains=("sleep",))
        assert "short_sleep" not in {f["kind"] for f in found}

    def test_a_baseline_rule_reads_this_person_not_a_norm(self):
        # A short sleeper whose nights are all ~6h20 is CONSISTENT, so the
        # against-my-own-average rule must stay quiet even though every night is
        # under the absolute 7h line.
        days = days_from(sleep_mins=[380, 382, 378, 381, 379] * 6)
        found = domain_findings.build_findings(days, domains=("sleep",))
        assert "sleep_below_own_average" not in {f["kind"] for f in found}

    def test_bedtime_spread_is_measured_around_the_clock(self):
        # 23:50 and 00:10 are twenty minutes apart, not twenty-three hours. A
        # linear standard deviation would call this the most irregular sleeper
        # alive; the circular one must call it rock steady.
        times = ["23:50", "00:10", "23:55", "00:05", "00:00"] * 4
        days = days_from(sleep_start=times)
        found = domain_findings.build_findings(days, domains=("sleep",))
        assert "irregular_bedtime" not in {f["kind"] for f in found}

    def test_a_genuinely_scattered_bedtime_is_caught(self):
        times = ["22:00", "01:30", "23:00", "02:00", "21:30"] * 4
        days = days_from(sleep_start=times)
        found = domain_findings.build_findings(days, domains=("sleep",))
        hit = next(f for f in found if f["kind"] == "irregular_bedtime")
        assert hit["evidence"]["spread_mins"] >= 60


class TestTrainingAndBody:
    def test_no_logged_workouts_at_all_is_a_data_gap_not_a_finding(self):
        # Someone who simply doesn't use the workout feature must not be nagged
        # every day about a gap that is really a missing integration.
        days = days_from(steps=[8000] * 30)
        found = domain_findings.build_findings(days, domains=("activity",))
        assert not [f for f in found if f["kind"] == "training_gap"]

    def test_a_gap_after_real_training_is_a_finding(self):
        counts = [1, 0, 1, 0, 1, 0, 1] + [0] * 8
        types = ["strength training", "", "strength training", "", "walking",
                 "", "strength training"] + [""] * 8
        days = days_from(workout_count=counts, workout_types=types)
        found = domain_findings.build_findings(days, domains=("activity",))
        hit = next(f for f in found if f["kind"] == "training_gap")
        assert hit["evidence"]["days_since_last"] == 8

    def test_recomposition_working_is_reported_as_good_news(self):
        # A coach that only ever reports problems is one the user stops opening.
        lean = [56.0] * 10 + [56.1] * 10
        fat = [22.0] * 10 + [20.8] * 10
        days = days_from(lean_mass_kg=lean, body_fat_pct=fat)
        found = domain_findings.build_findings(days, domains=("body",))
        hit = next(f for f in found if f["kind"] == "recomposition_working")
        assert hit["evidence"]["good_news"] is True

    def test_losing_lean_mass_outranks_the_good_news_branch(self):
        lean = [57.0] * 10 + [56.0] * 10
        fat = [22.0] * 10 + [21.0] * 10
        days = days_from(lean_mass_kg=lean, body_fat_pct=fat)
        found = domain_findings.build_findings(days, domains=("body",))
        kinds = {f["kind"] for f in found}
        assert "losing_lean_mass" in kinds
        assert "recomposition_working" not in kinds


class TestDigestion:
    def test_days_with_no_self_report_are_not_read_as_constipation(self):
        # bowel_movement is blank-means-no, but only on a day the user logged
        # something. A stretch where they stopped noting must stay silent.
        days = days_from(bowel_movement=[""] * 20)
        assert domain_findings.build_findings(days, domains=("digestion",)) == []

    def test_a_genuinely_low_rate_is_a_finding(self):
        flags = ["TRUE", "", "FALSE", "FALSE", "TRUE", "FALSE", "FALSE",
                 "FALSE", "TRUE", "FALSE", "FALSE", "FALSE", "TRUE", "FALSE"]
        days = days_from(bowel_movement=flags, total_fiber_g=[18] * 14)
        hit = next(f for f in domain_findings.build_findings(
            days, domains=("digestion",)) if f["kind"] ==
            "infrequent_bowel_movements")
        assert hit["evidence"]["average_fiber_g"] == 18.0


class TestPerDomainBudget:
    def test_one_domain_cannot_take_every_slot(self):
        days = days_from(
            sleep_mins=[470] * 14 + [340] * 10,
            sleep_efficiency_pct=[95] * 14 + [70] * 10,
            sleep_latency_mins=[10] * 14 + [55] * 10,
            steps=[9000] * 14 + [2000] * 10)
        found = domain_findings.build_findings(days, limit_per_domain=2)
        assert sum(1 for f in found if f["domain"] == "sleep") <= 2


class TestCausalAlignment:
    """The off-by-one that would silently invert every link."""

    def test_the_vocabulary_matches_the_registry(self):
        # links.py mirrors the causal constants as bare strings because it is
        # flattened next to main.py in the image. If the registry ever renames
        # one, this is what catches it.
        assert links.CAUSAL_INPUT == CAUSAL_INPUT
        assert links.CAUSAL_OUTCOME == CAUSAL_OUTCOME

    def test_food_meets_the_night_that_follows_it(self):
        # total_cals_in is a waking-day INPUT; sleep_deep_mins is the night that
        # ENDED that morning. The dinner on row N is followed by the sleep on N+1.
        assert links._offset(BY_NAME["total_cals_in"].causal,
                             BY_NAME["sleep_deep_mins"].causal) == 1

    def test_a_night_precedes_the_same_rows_eating(self):
        # ...and the reverse is NOT symmetric: the night on row N happened before
        # that same day's food, so the pair is on one row.
        assert links._offset(BY_NAME["sleep_mins"].causal,
                             BY_NAME["total_cals_in"].causal) == 0

    def test_two_inputs_are_contemporaneous(self):
        assert links._offset(BY_NAME["total_cals_in"].causal,
                             BY_NAME["steps"].causal) == 0

    def test_every_declared_link_has_a_defined_direction(self):
        columns = {c: BY_NAME[c].causal for c in BY_NAME}
        for link in links.LINKS:
            cause = links.causal_of(link.cause, columns)
            effect = links.causal_of(link.effect, columns)
            assert cause, f"{link.id}: unknown cause {link.cause}"
            assert effect, f"{link.id}: unknown effect {link.effect}"
            assert links._offset(cause, effect) is not None, link.id

    def test_every_declared_link_carries_a_mechanism(self):
        # The mechanism is what lets a card say WHY rather than just THAT, and it
        # is the only explanation the model is allowed to use.
        for link in links.LINKS:
            assert link.mechanism.strip(), link.id
            assert link.expect in (links.UP, links.DOWN), link.id
            assert link.blocks, link.id


class TestLinkEngine:
    COLUMNS = {c: BY_NAME[c].causal for c in BY_NAME}

    def _run(self, days, **kw):
        kw.setdefault("columns", self.COLUMNS)
        kw.setdefault("blocks", ("nutrition", "sleep", "activity", "body",
                                 "self_report"))
        return links.evaluate(days, **kw)

    def test_pure_noise_produces_no_links(self):
        # The whole point of the FDR correction: run this every day and it must
        # not invent a chain most weeks.
        import random
        rng = random.Random(7)
        days = days_from(
            total_cals_in=[rng.uniform(1800, 2600) for _ in range(90)],
            sleep_deep_mins=[rng.uniform(60, 110) for _ in range(90)],
            sleep_mins=[rng.uniform(380, 500) for _ in range(90)],
            steps=[rng.uniform(4000, 14000) for _ in range(90)])
        assert self._run(days) == []

    @staticmethod
    def _big_days_then_broken_nights(n=90, noise=3.0, seed=11):
        """A day of heavy intake, then a fragmented night — the effect landing on
        the FOLLOWING row, which is the only place a correctly aligned engine can
        find it. Exercises the declared `big_day_awakenings` hypothesis."""
        import random
        rng = random.Random(seed)
        cals, awakenings = [], []
        for i in range(n):
            big = i % 3 == 0
            cals.append((3200 if big else 1900) + rng.uniform(-80, 80))
            after_big = i > 0 and (i - 1) % 3 == 0
            awakenings.append((9 if after_big else 2) + rng.uniform(-noise, noise))
        return days_from(total_cals_in=cals, sleep_awakenings=awakenings)

    def test_a_real_effect_is_found_with_the_right_lag(self):
        found = self._run(self._big_days_then_broken_nights())
        hit = next(f for f in found if f["group"] == "big_day_awakenings")
        assert hit["evidence"]["cause"] == "total_cals_in"
        assert hit["evidence"]["effect"] == "sleep_awakenings"
        # The lag is what proves the alignment: the night is on the next row.
        assert hit["evidence"]["lag_days"] == 1
        assert hit["evidence"]["expected_direction"] == links.UP
        assert hit["evidence"]["effect_delta"] > 0
        assert hit["domain"] == "link"

    def test_an_effect_that_contradicts_its_mechanism_is_dropped(self):
        # Heavy days followed by unusually CALM nights. The hypothesis says the
        # opposite, and its mechanism would be quoted onto a card that showed the
        # reverse number — so the link must not be reported at all.
        import random
        rng = random.Random(23)
        cals, awakenings = [], []
        for i in range(90):
            big = i % 3 == 0
            cals.append((3200 if big else 1900) + rng.uniform(-80, 80))
            after_big = i > 0 and (i - 1) % 3 == 0
            awakenings.append((1 if after_big else 8) + rng.uniform(-1, 1))
        found = self._run(days_from(total_cals_in=cals,
                                    sleep_awakenings=awakenings))
        assert not [f for f in found if f["group"] == "big_day_awakenings"]

    def test_the_same_effect_is_invisible_when_the_lag_is_wrong(self):
        # The negative control for the alignment. Put the fragmented night on the
        # SAME row as the heavy day and the (correctly lagged) engine sees nothing —
        # which is exactly what would happen to a real effect if the offset were
        # hard-coded to 0.
        import random
        rng = random.Random(11)
        cals, awakenings = [], []
        for i in range(90):
            big = i % 3 == 0
            cals.append((3200 if big else 1900) + rng.uniform(-80, 80))
            awakenings.append((9 if big else 2) + rng.uniform(-3, 3))
        found = self._run(days_from(total_cals_in=cals,
                                    sleep_awakenings=awakenings))
        assert not [f for f in found if f["group"] == "big_day_awakenings"]

    def test_a_link_is_never_claimed_as_causation(self):
        found = self._run(self._big_days_then_broken_nights(noise=1.0))
        assert found
        for finding in found:
            assert finding["evidence"]["claim"] == "association"
            assert "association, not proof" in finding["headline"]
            assert finding["evidence"]["mechanism"]

    def test_capability_gating_silences_the_engine_entirely(self):
        # A phone-only friend gets no links at all — absent, not silenced.
        days = self._big_days_then_broken_nights(noise=1.0)
        assert self._run(days) != []                        # the effect is there
        assert self._run(days, blocks=("nutrition", "self_report")) == []

    def test_below_the_minimum_n_nothing_is_evaluated(self):
        assert self._run(self._big_days_then_broken_nights(n=12)) == []

    def test_the_engine_is_deterministic(self):
        # A card must not appear and vanish between two refreshes of the same day.
        days = self._big_days_then_broken_nights()
        assert self._run(days) == self._run(days)

    def test_a_meal_derived_feature_can_drive_a_link(self):
        # The features exist because the daily roll-up flattens away the timestamps:
        # "calories after 21:00" is not a column and never can be.
        import random
        rng = random.Random(17)
        deep, features = [], {}
        rows = []
        for i in range(90):
            day = f"2026-06-{i + 1:02d}"
            late = i % 2 == 0
            features[day] = {"calories_after_21h": 800.0 if late else 0.0}
            after_late = i > 0 and (i - 1) % 2 == 0
            deep.append((55 if after_late else 100) + rng.uniform(-4, 4))
            rows.append({"date": day, "sleep_deep_mins": deep[-1]})
        found = self._run(rows, features_by_day=features)
        hit = next(f for f in found if f["group"] == "late_calories_deep_sleep")
        assert hit["evidence"]["lag_days"] == 1
        assert hit["evidence"]["effect_delta"] < 0


class TestBenjaminiHochberg:
    def test_nothing_survives_when_everything_is_null(self):
        assert links._benjamini_hochberg([0.4, 0.6, 0.9, 0.8]) == [False] * 4

    def test_a_single_strong_result_survives(self):
        keep = links._benjamini_hochberg([0.0001, 0.6, 0.9, 0.8])
        assert keep[0] is True and not any(keep[1:])

    def test_it_is_less_permissive_than_a_bare_threshold(self):
        # 0.001 and 0.04 would both pass an uncorrected p<0.05 gate. BH keeps only
        # the first: the second is exactly the marginal "finding" that, run daily
        # across two dozen hypotheses, would have the coach inventing a chain most
        # weeks.
        pvalues = [0.001, 0.04, 0.06]
        keep = links._benjamini_hochberg(pvalues)
        assert keep == [True, False, False]
        assert sum(1 for p in pvalues if p < 0.05) == 2   # what naive would take


class TestMealFeatures:
    def test_a_day_with_no_meals_produces_no_entry(self):
        # Not zeros: "ate nothing after 21:00" and "logged nothing" are different
        # facts, and conflating them manufactures correlations out of forgetfulness.
        assert links.daily_features({"2026-06-01": []}) == {}

    def test_late_eating_is_measured_past_midnight(self):
        # A 00:30 dessert belongs to the evening it followed, and must count as
        # late (24.5h), not as an early breakfast.
        feats = links.daily_features({"2026-06-01": [
            {"datetime": "2026-06-01 13:00", "calories": 700, "fat_g": 20},
            {"datetime": "2026-06-02 00:30", "calories": 300, "fat_g": 15},
        ]})["2026-06-01"]
        assert feats["calories_after_21h"] == 300
        assert feats["fat_g_after_21h"] == 15
        assert feats["last_meal_hour"] == pytest.approx(24.5)

    def test_every_declared_feature_is_computable(self):
        feats = links.daily_features({"2026-06-01": [
            {"datetime": "2026-06-01 08:00", "calories": 400, "protein_g": 30,
             "fat_g": 10},
            {"datetime": "2026-06-01 22:00", "calories": 900, "protein_g": 40,
             "fat_g": 35},
        ]})["2026-06-01"]
        for name in links.FEATURES:
            assert name in feats, name
