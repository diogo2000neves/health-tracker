"""Strength-training roll-up: the guards, the arithmetic and the one-line summary.

Two things these protect, and both are the reason the feature is worth having at
all. First, that a misread number off a phone screen never becomes a baseline —
once a wrong load is in the history, every later session is scored against it.
Second, that the day's index means what it claims: warm-ups excluded, bodyweight
work counted honestly, and the comparison made against days that came BEFORE this
one.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ingest"))

workouts = importlib.import_module("workouts")


def s(exercise="bench press", reps=8, weight=60.0, rir=2, set_type="normal",
      load_type="external", **extra):
    """One set, with the boring fields filled in."""
    entry = {"exercise": exercise, "reps": reps, "weight_kg": weight,
             "set_type": set_type, "load_type": load_type}
    if rir is not None:
        entry["rir"] = rir
    entry.update(extra)
    return entry


class TestNormalisation:
    def test_a_load_outside_the_band_is_dropped_not_written(self):
        # The scale path's lesson: a dropped decimal reads as 7005 kg and sails
        # into the sheet. Here it would become an unbeatable baseline forever.
        kept = workouts.normalize_sets([s(weight=6000.0), s(weight=60.0)])
        assert [e["weight_kg"] for e in kept] == [60.0]

    def test_impossible_reps_drop_the_set(self):
        assert workouts.normalize_sets([s(reps=0)]) == []
        assert workouts.normalize_sets([s(reps=500)]) == []

    def test_an_implausible_rir_unknowns_the_field_but_keeps_the_set(self):
        # The set genuinely happened; only the RIR is unreadable. Dropping the whole
        # set would lose real volume over one soft field.
        kept = workouts.normalize_sets([s(rir=47)])
        assert len(kept) == 1 and "rir" not in kept[0]

    def test_pounds_are_converted_here_not_by_the_model(self):
        kept = workouts.normalize_sets([s(weight=100.0)], unit="lb")
        assert kept[0]["weight_kg"] == pytest.approx(45.36, abs=0.01)

    def test_a_bodyweight_set_with_no_load_survives(self):
        # weight_kg = 0 is legitimate here, unlike portion_g = 0 on the food path.
        kept = workouts.normalize_sets(
            [s(exercise="pull up", weight=0.0, load_type="bodyweight")])
        assert len(kept) == 1 and kept[0]["load_type"] == "bodyweight"

    def test_an_external_set_with_no_readable_load_is_dropped(self):
        assert workouts.normalize_sets([s(weight=None)]) == []

    def test_a_set_with_no_exercise_name_is_not_a_set(self):
        assert workouts.normalize_sets([s(exercise="  ")]) == []

    def test_booleans_and_nan_are_not_numbers(self):
        assert workouts.normalize_sets([s(weight=True)]) == []
        assert workouts.normalize_sets([s(reps=float("nan"))]) == []

    def test_unknown_type_labels_fall_back_rather_than_crash(self):
        kept = workouts.normalize_sets([s(set_type="superset", load_type="magic")])
        assert kept[0]["set_type"] == "normal"
        assert kept[0]["load_type"] == "external"

    def test_a_redundant_portuguese_name_is_not_stored_twice(self):
        kept = workouts.normalize_sets(
            [s(exercise="supino", exercise_pt="supino"), s(exercise_pt="supino")])
        assert "exercise_pt" not in kept[0]
        assert kept[1]["exercise_pt"] == "supino"

    def test_garbage_input_is_empty_not_an_exception(self):
        assert workouts.normalize_sets(None) == []
        assert workouts.normalize_sets("four sets") == []
        assert workouts.normalize_sets([None, 7, {"reps": 8}]) == []


class TestCounting:
    def test_warm_ups_are_recorded_but_never_scored(self):
        sets = [s(set_type="warmup", weight=20.0), s(), s()]
        assert len(workouts.working_sets(sets)) == 2

    def test_a_set_without_a_recorded_rir_is_not_a_hard_set(self):
        # Biased low on purpose: telling the user they hit a volume target they
        # missed is the one error that makes the number worthless.
        assert workouts.hard_sets([s(rir=None)]) == []

    def test_hard_sets_are_the_ones_within_two_of_failure(self):
        sets = [s(rir=0), s(rir=2), s(rir=3), s(rir=2, set_type="warmup")]
        assert len(workouts.hard_sets(sets)) == 2


class TestEffectiveLoad:
    def test_a_pull_up_is_not_a_zero_kilo_lift(self):
        entry = s(exercise="pull up", weight=0.0, load_type="bodyweight")
        assert workouts.effective_load_kg(entry, 70.0) == 70.0

    def test_added_load_stacks_on_bodyweight(self):
        entry = s(weight=10.0, load_type="bodyweight")
        assert workouts.effective_load_kg(entry, 70.0) == 80.0

    def test_assistance_is_subtracted(self):
        entry = s(weight=20.0, load_type="assisted")
        assert workouts.effective_load_kg(entry, 70.0) == 50.0

    def test_without_a_weigh_in_bodyweight_work_stays_out_rather_than_guessing(self):
        entry = s(weight=0.0, load_type="bodyweight")
        assert workouts.effective_load_kg(entry, None) is None

    def test_a_band_has_no_readable_resistance(self):
        assert workouts.effective_load_kg(s(load_type="band"), 70.0) is None


class TestLoadIndex:
    def test_matching_your_baseline_reads_as_one_hundred(self):
        assert workouts.load_index({"bench": 100.0}, {"bench": 100.0}) == 100.0

    def test_seven_percent_above_baseline_reads_as_107(self):
        assert workouts.load_index({"bench": 107.0}, {"bench": 100.0}) == 107.0

    def test_a_brand_new_exercise_is_left_out_rather_than_scored_as_average(self):
        # Scoring it 100 would drag every session that introduces a movement
        # toward the middle, hiding a real gain on the exercises that do have a
        # baseline.
        index = workouts.load_index(
            {"bench": 110.0, "new lift": 50.0}, {"bench": 100.0})
        assert index == 110.0

    def test_no_overlap_at_all_is_blank_not_zero(self):
        # "No baseline" and "far below baseline" are different facts.
        assert workouts.load_index({"bench": 100.0}, {}) is None

    def test_drop_sets_do_not_drag_the_index_down(self):
        # A drop set is performed pre-fatigued, so its e1RM understates the lift.
        sets = [s(weight=100.0, reps=5), s(weight=40.0, reps=8, set_type="drop")]
        best = workouts.best_e1rm_by_exercise(sets)
        assert best["bench press"] == pytest.approx(workouts.e1rm(100.0, 5), abs=0.01)

    def test_a_very_high_rep_set_does_not_estimate_a_one_rep_max(self):
        assert workouts.best_e1rm_by_exercise([s(reps=30, weight=10.0)]) == {}


class TestBaselineWindow:
    def history(self):
        return [
            {"date": "2026-07-01", "e1rms": {"bench": 90.0}},   # too old
            {"date": "2026-08-01", "e1rms": {"bench": 100.0}},
            {"date": "2026-08-10", "e1rms": {"bench": 95.0}},
            {"date": "2026-08-21", "e1rms": {"bench": 130.0}},  # today
        ]

    def test_today_is_excluded_so_the_index_is_out_of_sample(self):
        # Include today and the session can only ever score as average — the same
        # discipline src/calibration.py insists on for the energy-balance fit.
        base = workouts.baseline_e1rms(self.history(), upto_date="2026-08-21")
        assert base == {"bench": 100.0}

    def test_days_beyond_the_window_do_not_count(self):
        base = workouts.baseline_e1rms(self.history(), upto_date="2026-08-21",
                                       days=15)
        assert base == {"bench": 95.0}

    def test_a_broken_date_is_skipped_not_fatal(self):
        base = workouts.baseline_e1rms(
            [{"date": "not-a-date", "e1rms": {"bench": 999.0}}],
            upto_date="2026-08-21")
        assert base == {}


class TestSummaryLine:
    def test_identical_sets_collapse_to_one_group(self):
        sets = [s(exercise="supino", weight=60.0, reps=8) for _ in range(4)]
        line = workouts.summary_line("Tronco A", sets)
        assert "supino 4×8@60" in line
        assert line.startswith("Tronco A · ")

    def test_a_ramp_shows_its_range_rather_than_pretending_it_was_flat(self):
        sets = [s(exercise="supino", weight=60.0, reps=10),
                s(exercise="supino", weight=70.0, reps=8)]
        assert "supino 2×10-8@60-70" in workouts.summary_line("", sets)

    def test_bodyweight_work_reads_as_bodyweight(self):
        sets = [s(exercise="elevações", weight=0.0, load_type="bodyweight"),
                s(exercise="elevações", weight=5.0, load_type="bodyweight")]
        assert "elevações 2×8@PC-PC+5" in workouts.summary_line("", sets)

    def test_warm_ups_stay_out_of_the_line(self):
        sets = [s(exercise="supino", weight=20.0, set_type="warmup"),
                s(exercise="supino", weight=60.0)]
        assert "20" not in workouts.summary_line("", sets)

    def test_the_tail_counts_sets_and_averages_rir(self):
        sets = [s(rir=2), s(rir=3)]
        line = workouts.summary_line("", sets)
        assert "2 séries (1 duras)" in line and "RIR 2,5" in line

    def test_a_session_with_only_warm_ups_is_just_its_title(self):
        assert workouts.summary_line("Tronco A",
                                     [s(set_type="warmup")]) == "Tronco A"

    def test_the_line_is_far_cheaper_than_the_same_data_as_json(self):
        # The measured argument in CONTEXT.md §2b, re-checked on real shape: this
        # column is read on every row of every export and every coach prompt.
        import json
        sets = [s(exercise=name, weight=w, reps=8)
                for name, w in (("supino", 60.0), ("remada", 45.0),
                                ("desenvolvimento", 22.0))
                for _ in range(4)]
        line = workouts.summary_line("Tronco A", sets)
        assert len(line) * 4 < len(json.dumps(sets))


class TestDailyRow:
    def test_a_day_with_no_session_writes_nothing(self):
        assert workouts.daily_row([]) == {}

    def test_a_warm_up_only_session_writes_nothing(self):
        sessions = [{"title": "A", "sets": [s(set_type="warmup")]}]
        assert workouts.daily_row(sessions) == {}

    def test_two_sessions_in_a_day_fold_together_rather_than_overwrite(self):
        sessions = [{"title": "Tronco A", "sets": [s(), s()]},
                    {"title": "Inferior", "sets": [s(exercise="agachamento")]}]
        row = workouts.daily_row(sessions)
        assert row["lift_sets"] == 3
        assert row["lift_session"] == "Tronco A + Inferior"
        assert "Tronco A" in row["lift_summary"] and "Inferior" in row["lift_summary"]

    def test_session_minutes_sum_across_sessions_the_same_day(self):
        sessions = [{"title": "Tronco A", "sets": [s()], "duration_min": 42},
                    {"title": "Cardio", "sets": [s(exercise="remada")],
                    "duration_min": 18.7}]
        assert workouts.daily_row(sessions)["lift_mins"] == 61  # rounded, not floored

    def test_a_missing_duration_is_blank_not_zero(self):
        # A session screenshot legitimately carries no duration (cropped, or the
        # app didn't show it) — the column should be ABSENT, not a false 0 min.
        row = workouts.daily_row([{"title": "A", "sets": [s()]}])
        assert "lift_mins" not in row

    def test_the_index_lands_when_there_is_history_to_compare_against(self):
        sessions = [{"title": "A", "sets": [s(exercise="supino", weight=110.0,
                                              reps=5)]}]
        history = [{"date": "2026-08-01",
                    "e1rms": {"supino": workouts.e1rm(100.0, 5)}}]
        row = workouts.daily_row(sessions, history=history, date="2026-08-21")
        assert row["lift_load_index"] == pytest.approx(110.0, abs=0.1)

    def test_without_history_the_index_is_absent_rather_than_a_made_up_number(self):
        sessions = [{"title": "A", "sets": [s()]}]
        row = workouts.daily_row(sessions, history=[], date="2026-08-21")
        assert "lift_load_index" not in row
        assert row["lift_sets"] == 1  # the rest of the row still lands

    def test_every_key_written_is_a_real_registry_column(self):
        from schema.registry import BY_NAME
        sessions = [{"title": "A", "sets": [s()]}]
        history = [{"date": "2026-08-01", "e1rms": {"bench press": 100.0}}]
        row = workouts.daily_row(sessions, history=history, date="2026-08-21")
        assert set(row) <= set(BY_NAME)
        for name in row:
            assert BY_NAME[name].block == "training"
