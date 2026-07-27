"""What each user measures, and what follows from it.

The feature these cover is "add a friend who only takes meal photos", so the tests
are written from that angle: everything that must go quiet for a phone-only user,
and everything that must not change for the owner.
"""
import pytest

from schema import capabilities as caps
from schema.registry import BLOCKS, DAILY_COLUMNS


class TestBlocksAndPresets:
    def test_toggleable_blocks_exclude_the_structural_ones(self):
        # `key` (the date) and `meta` (bookkeeping) exist for every user; making
        # them switchable would let a config typo produce rows with no date.
        assert "key" not in caps.TOGGLEABLE_BLOCKS
        assert "meta" not in caps.TOGGLEABLE_BLOCKS
        assert set(caps.TOGGLEABLE_BLOCKS) | {"key", "meta"} == set(BLOCKS)

    def test_every_preset_expands_to_real_blocks(self):
        for name, blocks in caps.PRESETS.items():
            assert set(blocks) <= set(caps.TOGGLEABLE_BLOCKS), name

    def test_presets_are_not_a_ladder(self):
        # The design rule: a friend with a watch but no scale is not "level 2.5".
        # nutrition_activity and nutrition_sleep must be genuinely different sets,
        # neither one contained in the other.
        sleep = set(caps.PRESETS["nutrition_sleep"])
        activity = set(caps.PRESETS["nutrition_activity"])
        assert not sleep <= activity and not activity <= sleep

    def test_the_default_is_everything(self):
        # An existing deployment with no config tab must behave exactly as before.
        assert caps.FULL.blocks == caps.TOGGLEABLE_BLOCKS
        assert set(caps.FULL.visible_blocks()) == set(BLOCKS)


class TestWhatFollowsFromBlocks:
    def test_a_nutrition_only_user_needs_no_health_integration(self):
        friend = caps.from_preset("nutrition")
        assert not friend.needs_google_health()
        assert "fitbit" not in friend.sources()
        assert "scale" not in friend.sources()
        # ...and the owner still does.
        assert caps.FULL.needs_google_health()

    def test_columns_follow_the_blocks(self):
        friend = caps.from_preset("nutrition")
        names = friend.column_names()
        assert "total_protein_g" in names          # nutrition
        assert "date" in names and "updated_at" in names   # structural
        assert "sleep_mins" not in names
        assert "weight_kg" not in names
        assert "workout_mins" not in names

    def test_every_column_belongs_to_a_reachable_block(self):
        # A column in a block no preset can enable would be dead weight nobody
        # could ever see.
        reachable = set(caps.TOGGLEABLE_BLOCKS) | caps.STRUCTURAL_BLOCKS
        for column in DAILY_COLUMNS:
            assert column.block in reachable, column.name

    def test_domains_and_blind_spots_partition_the_world(self):
        friend = caps.from_preset("nutrition")
        assert "nutrition" in friend.domains()
        assert "sleep" in friend.blind_spots()
        assert "activity" in friend.blind_spots()
        assert "body" in friend.blind_spots()
        # digestion rides along with a phone-only setup: a bowel note needs no
        # hardware, only the text Shortcut the meals already use.
        assert "digestion" in friend.domains()
        assert not set(friend.domains()) & set(friend.blind_spots())
        assert caps.FULL.blind_spots() == ()

    def test_sleep_and_recovery_are_one_domain_to_a_reader(self):
        assert caps.BLOCK_DOMAINS["sleep"] == caps.BLOCK_DOMAINS["recovery"]

    def test_a_link_needs_every_block_it_spans(self):
        friend = caps.from_preset("nutrition")
        assert not friend.can_link("nutrition", "sleep")
        assert friend.can_link("nutrition", "self_report")
        assert caps.FULL.can_link("nutrition", "sleep", "body")


class TestTheDeclaredBody:
    def test_mifflin_st_jeor_for_a_declared_profile(self):
        friend = caps.from_preset(
            "nutrition", sex="male", age=30, height_cm=180.0,
            declared_weight_kg=80.0, activity_level="moderate")
        # 10*80 + 6.25*180 - 5*30 + 5 = 1780
        assert friend.basal_metabolic_rate() == pytest.approx(1780.0)
        assert friend.declared_tdee() == pytest.approx(1780.0 * 1.55)

    def test_an_incomplete_profile_declares_nothing(self):
        # Half a profile must not produce a confident number; the caller falls
        # through to its own constant instead.
        assert caps.from_preset("nutrition", sex="male", age=30).declared_tdee() is None
        assert caps.FULL.basal_metabolic_rate() is None

    def test_sex_changes_the_constant(self):
        common = dict(age=30, height_cm=170.0, declared_weight_kg=65.0)
        male = caps.Capabilities(sex="male", **common).basal_metabolic_rate()
        female = caps.Capabilities(sex="female", **common).basal_metabolic_rate()
        assert male - female == pytest.approx(166.0)   # +5 vs -161


class TestReadingTheConfigTab:
    def test_a_preset_name(self):
        got = caps.from_config([{"key": "blocks", "value": "nutrition"}])
        assert got.preset == "nutrition"
        assert set(got.blocks) == set(caps.PRESETS["nutrition"])

    def test_an_explicit_list(self):
        got = caps.from_config([{"key": "blocks", "value": "nutrition, sleep"}])
        assert set(got.blocks) == {"nutrition", "sleep"}
        assert got.preset is None

    def test_an_empty_tab_means_everything(self):
        assert caps.from_config([]).blocks == caps.TOGGLEABLE_BLOCKS

    def test_a_typo_never_takes_the_service_down(self):
        # Hand-edited spreadsheet: every bad value falls back rather than raising.
        got = caps.from_config([
            {"key": "blocks", "value": "nutrition, slep, activty"},
            {"key": "goal", "value": "become a wizard"},
            {"key": "sex", "value": "yes"},
            {"key": "age", "value": "old"},
            {"key": "activity_level", "value": "very sporty indeed"},
        ])
        assert set(got.blocks) == {"nutrition"}    # the one real name survives
        assert got.goal == caps.DEFAULT_GOAL
        assert got.sex is None and got.age is None
        assert got.activity_level == caps.DEFAULT_ACTIVITY_LEVEL

    def test_a_list_that_matches_nothing_falls_back_to_nutrition(self):
        # Not to "everything off": a user with no blocks at all has no app, and a
        # typo must never be the thing that produces that.
        got = caps.from_config([{"key": "blocks", "value": "slep, activty"}])
        assert set(got.blocks) == set(caps.PRESETS["nutrition"])

    def test_european_decimals_survive(self):
        # The sheet's locale renders decimals with commas (gotcha 10).
        got = caps.from_config([{"key": "height_cm", "value": "178,5"}])
        assert got.height_cm == pytest.approx(178.5)

    def test_the_seed_covers_every_key_the_reader_understands(self):
        # A key the reader supports but never seeds is one a user can't discover.
        seeded = {row[0] for row in caps.CONFIG_SEED}
        assert seeded == {"blocks", "goal", "sex", "age", "height_cm",
                          "weight_kg", "activity_level"}

    def test_the_api_shape_carries_what_the_app_draws_from(self):
        payload = caps.from_preset("nutrition").to_api()
        # Normalised to the registry's own declaration order, not the order the
        # preset happens to list them in, so the payload is stable.
        assert set(payload["blocks"]) == set(caps.PRESETS["nutrition"])
        assert payload["blocks"] == [b for b in caps.TOGGLEABLE_BLOCKS
                                     if b in set(payload["blocks"])]
        assert "sleep" in payload["blind_spots"]
        assert payload["goal_label_pt"]
