"""Which model answers which prompt — the three routing layers.

What these protect, in order of how expensive getting it wrong is:

* **A spent Claude window must not cost a meal.** The whole point of a provider
  chain is that the failure is invisible; if failover breaks, ingest falls all the
  way to the Gemini API and the accuracy decision in CONTEXT.md §6 is quietly
  undone with only a log line to show for it.
* **A downgrade must never be silent where it matters.** The meal estimate stays on
  the deep tier, and an unclassified new call site defaults to deep rather than to
  cheap.
* **A hand-edited spreadsheet cell must not be able to break routing.** `llm_mode`
  comes from the `config` tab, so a typo has to read as "auto".
"""
import importlib
import os
import sys

import pytest

_AUTOMATION = os.path.join(os.path.dirname(__file__), "..", "..",
                           "automation", "nutrition-audit")
if _AUTOMATION not in sys.path:
    sys.path.insert(0, _AUTOMATION)

claude_cli = importlib.import_module("claude_cli")
llm_cli = importlib.import_module("llm_cli")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No inherited mode, no inherited pin, no cooldown left over from another test."""
    monkeypatch.delenv("LLM_MODE", raising=False)
    monkeypatch.setattr(llm_cli, "PRIMARY_MODEL", "")
    llm_cli.reset_cooldown()
    yield
    llm_cli.reset_cooldown()


def _providers(chain):
    return [(p.kind, p.model) for p in chain]


# -- layer 1: per-task defaults ------------------------------------------------
class TestRoutes:
    def test_the_meal_estimate_stays_on_the_deep_tier(self):
        # CONTEXT.md §2e has already MEASURED a ~250 kcal/day systematic bias.
        # This is the number the project exists to find; it does not go to flash
        # to save a few seconds.
        assert llm_cli.tier_for("ingest.claude_estimator") == "deep"
        assert llm_cli.chain_for("ingest.claude_estimator")[0].kind == llm_cli.CLAUDE

    def test_the_coach_stays_on_the_deep_tier(self):
        # worker.py's own docstring: the coaching voice is the product. This
        # generic "coach" source is what chat/report fall back to for logging —
        # they always pin their own model, so it never actually picks who answers.
        assert llm_cli.tier_for("coach") == "deep"

    def test_the_coach_card_leads_with_gemini(self):
        # Only the plain daily/weekly card reaches llm_cli with no model of its
        # own (see worker.answer) — chat and report pin COACH_CHAT_MODEL /
        # COACH_REPORT_MODEL and bypass routing entirely, so this is the one
        # coach call Diogo's "Gemini first" choice actually touches.
        assert llm_cli.tier_for("coach.card") == "card"
        chain = llm_cli.chain_for("coach.card")
        assert chain[0].is_gemini
        assert chain[0].effort == "high"
        assert chain[1].kind == llm_cli.CLAUDE

    def test_transcription_goes_to_the_fast_tier(self):
        for source in ("ingest.body_ocr", "ingest.workout_ocr"):
            assert llm_cli.tier_for(source) == "fast"
        assert llm_cli.chain_for("ingest.body_ocr")[0].is_gemini

    def test_the_image_router_gets_its_own_cheapest_tier(self):
        assert llm_cli.tier_for("ingest.image_router") == "classify"
        assert llm_cli.chain_for("ingest.image_router")[0].effort == "low"

    def test_transcription_thinks_harder_than_classification(self):
        """A gym screen is a dense grid of near-identical rows, and a misread rep
        count lands INSIDE workouts.SET_RANGES — so the plausibility band cannot
        catch it and effort is the only guard. Classification just names the
        picture."""
        assert llm_cli.TIERS["fast"][0].effort == "medium"
        assert llm_cli.TIERS["classify"][0].effort == "low"

    def test_every_agy_provider_names_an_effort_the_cli_accepts(self):
        """A bare model id with no effort is a hard CLI error — exactly how this
        broke on the first live call. Either the id carries the suffix or the
        effort field does."""
        import agy_cli
        for tier, providers in llm_cli.TIERS.items():
            for p in providers:
                if not p.is_gemini:
                    continue
                suffixed = any(p.model.endswith(f"-{e}") for e in agy_cli.EFFORTS)
                assert suffixed or p.effort in agy_cli.EFFORTS, \
                    f"{tier}: {p.model!r} would be rejected by agy"

    def test_a_text_note_is_judgement_not_transcription(self):
        # There is no screen to read: it is a meal described in words (or a bowel
        # log), and estimating from a description is exactly the judgement the deep
        # tier exists for. `ingest.meal_estimate` is what images fall back to when
        # classification fails or is unavailable; it stays deep for the same
        # reason — see test_a_text_note_leads_with_gemini_not_claude below for the
        # source the "send a note" shortcut actually uses.
        assert llm_cli.tier_for("ingest.meal_estimate") == "deep"

    def test_a_text_note_leads_with_gemini_not_claude(self):
        # The "send a note" shortcut carries no photo, so there is nothing for
        # Claude's vision to earn its latency on — Diogo's notes are short and
        # specific ("100g de arroz e um ovo"), and Gemini answers them just as well
        # for a fraction of the 5-hour window. Still high effort and still falls
        # back to Claude: it is judgement, same as any other meal estimate, just
        # not one that needs the strongest model first. See llm_cli.NOTE.
        assert llm_cli.tier_for("ingest.text_note") == "note"
        chain = llm_cli.chain_for("ingest.text_note")
        assert chain[0].is_gemini
        assert chain[0].effort == "high"
        assert chain[1].kind == llm_cli.CLAUDE

    def test_an_unknown_source_gets_the_good_model_not_the_cheap_one(self):
        # A new call site nobody has classified should show up as a cost, not as a
        # quality regression that takes weeks to notice.
        assert llm_cli.tier_for("something.brand.new") == llm_cli.DEFAULT_TIER
        assert llm_cli.tier_for("") == llm_cli.DEFAULT_TIER

    def test_every_declared_route_names_a_real_tier(self):
        for source, tier in llm_cli.ROUTES.items():
            assert tier in llm_cli.TIERS, f"{source} routes to a missing tier"

    def test_every_tier_ends_up_somewhere(self):
        for name, providers in llm_cli.TIERS.items():
            assert providers, f"{name} has no providers"
            for p in providers:
                assert p.kind in (llm_cli.CLAUDE, llm_cli.AGY)


# -- layer 2: automatic failover -----------------------------------------------
class TestFailover:
    def test_a_spent_claude_window_is_answered_by_agy_not_lost(self, monkeypatch):
        def spent(*a, **k):
            raise claude_cli.ClaudeError("usage limit reached")

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", spent)
        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json",
                            lambda *a, **k: {"kind": "meal", "_model_id": "agy:x"})
        answer = llm_cli.call_json("p", timeout_s=10, source="ingest.claude_estimator")
        assert answer["_model_id"] == "agy:x"

    def test_when_every_provider_fails_the_caller_still_sees_one_error_type(
            self, monkeypatch):
        """ingest falls through on any exception and worker.py only catches
        ClaudeError — a different type here would crash the coach's oneshot unit
        instead of releasing the job."""
        def dead(*a, **k):
            raise claude_cli.ClaudeError("nope")

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", dead)
        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json", dead)
        with pytest.raises(claude_cli.ClaudeError):
            llm_cli.call_json("p", timeout_s=10, source="coach")

    def test_the_chain_stops_at_the_first_provider_that_answers(self, monkeypatch):
        calls = []

        def claude(*a, **k):
            calls.append("claude")
            return {"kind": "meal"}

        def agy(*a, **k):
            calls.append("agy")
            return {"kind": "meal"}

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", claude)
        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json", agy)
        llm_cli.call_json("p", timeout_s=10, source="coach")
        assert calls == ["claude"]

    def test_a_claude_failure_arms_a_cooldown_so_the_next_call_skips_it(
            self, monkeypatch):
        # Otherwise every meal for the rest of the window burns ~30 s of
        # subprocess before failing the same way.
        def spent(*a, **k):
            raise claude_cli.ClaudeError("usage limit reached")

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", spent)
        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json",
                            lambda *a, **k: {"kind": "meal"})
        llm_cli.call_json("p", timeout_s=10, source="coach")
        assert llm_cli.claude_in_cooldown()
        assert all(not p.kind == llm_cli.CLAUDE
                   for p in llm_cli.chain_for("coach"))

    def test_the_cooldown_expires_on_its_own_rather_than_on_a_retry(self,
                                                                    monkeypatch):
        """It cannot clear on a successful Claude call, because while it is armed
        Claude is never called. Time is the only thing that releases it — which is
        the point: the 5-hour window reopens whether or not we poke at it."""
        llm_cli.note_failure(llm_cli.Provider(llm_cli.CLAUDE, "claude-sonnet-5"))
        assert llm_cli.claude_in_cooldown()
        base = llm_cli.time.monotonic()
        monkeypatch.setattr(llm_cli.time, "monotonic",
                            lambda: base + llm_cli.COOLDOWN_S + 1)
        assert not llm_cli.claude_in_cooldown()
        assert llm_cli.chain_for("coach")[0].kind == llm_cli.CLAUDE

    def test_a_claude_answer_outside_a_cooldown_keeps_it_clear(self, monkeypatch):
        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json",
                            lambda *a, **k: {"kind": "meal"})
        llm_cli.call_json("p", timeout_s=10, source="coach")
        assert not llm_cli.claude_in_cooldown()

    def test_an_agy_failure_does_not_arm_the_claude_cooldown(self):
        llm_cli.note_failure(llm_cli.Provider(llm_cli.AGY, "gemini-3.6-flash"))
        assert not llm_cli.claude_in_cooldown()

    def test_a_cooldown_never_empties_a_chain_that_has_no_alternative(
            self, monkeypatch):
        # Answering with the "wrong" provider beats not answering: the caller's
        # alternative is a failed meal, not a better one.
        monkeypatch.setitem(llm_cli.TIERS, "deep",
                            (llm_cli.Provider(llm_cli.CLAUDE, "claude-sonnet-5"),))
        llm_cli.note_failure(llm_cli.Provider(llm_cli.CLAUDE, "claude-sonnet-5"))
        assert _providers(llm_cli.chain_for("coach")) == [
            (llm_cli.CLAUDE, "claude-sonnet-5")]


# -- layer 3: the deliberate mode ----------------------------------------------
class TestMode:
    def test_economy_keeps_the_claude_window_for_something_else(self):
        chain = llm_cli.chain_for("ingest.claude_estimator", mode="economy")
        assert chain and all(p.is_gemini for p in chain)

    def test_quality_refuses_to_downgrade_silently(self):
        chain = llm_cli.chain_for("ingest.claude_estimator", mode="quality")
        assert len(chain) == 1 and chain[0].kind == llm_cli.CLAUDE

    def test_auto_is_the_full_chain_in_order(self):
        assert _providers(llm_cli.chain_for("coach", mode="auto")) == \
            _providers(llm_cli.TIERS["deep"])

    def test_a_typo_in_the_spreadsheet_cell_reads_as_auto(self):
        # This value is hand-typed into a Sheets cell. The worst a typo may do is
        # leave routing at its default — never take an ingest down.
        assert llm_cli.resolve_mode("ecconomy") == "auto"
        assert llm_cli.resolve_mode("") == "auto"
        assert llm_cli.resolve_mode(None) == "auto"
        assert llm_cli.resolve_mode("  ECONOMY  ") == "economy"

    def test_the_env_var_is_the_fallback_when_no_mode_is_passed(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "economy")
        assert llm_cli.resolve_mode() == "economy"
        assert llm_cli.resolve_mode("quality") == "quality"   # caller still wins

    def test_economy_beats_the_cooldown_rather_than_fighting_it(self):
        llm_cli.note_failure(llm_cli.Provider(llm_cli.CLAUDE, "claude-sonnet-5"))
        chain = llm_cli.chain_for("ingest.claude_estimator", mode="economy")
        assert all(p.is_gemini for p in chain)


# -- the escape hatches --------------------------------------------------------
class TestPinning:
    def test_an_explicit_model_bypasses_routing_entirely(self, monkeypatch):
        """The audit's ensemble is only meaningful if each estimate can be pinned
        to a named model — see CONTEXT.md §6 on re-independence."""
        seen = {}

        def claude(prompt, *, model, **k):
            seen["model"] = model
            return {"kind": "meal"}

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", claude)
        llm_cli.call_json("p", model="claude-opus-5", effort="low", timeout_s=10,
                          source="ingest.claude_estimator")
        assert seen["model"] == "claude-opus-5"

    def test_a_pinned_gemini_model_goes_to_agy(self, monkeypatch):
        seen = {}

        def agy(prompt, *, model, **k):
            seen["model"] = model
            return {"kind": "meal"}

        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json", agy)
        llm_cli.call_json("p", model="gemini-3.6-flash-high", timeout_s=10,
                          source="coach")
        assert seen["model"] == "gemini-3.6-flash-high"

    def test_primary_model_is_still_the_instant_rollback(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(llm_cli, "PRIMARY_MODEL", "claude-sonnet-5")

        def claude(prompt, *, model, **k):
            seen["model"] = model
            return {"kind": "meal"}

        monkeypatch.setattr(llm_cli.claude_cli, "call_claude_json", claude)
        # even a `fast` source is pinned when the global override is set
        llm_cli.call_json("p", timeout_s=10, source="ingest.body_ocr")
        assert seen["model"] == "claude-sonnet-5"


# -- the agy invocation contract (learned from a live failure, 2026-08-21) ------
class TestAgyEffort:
    def test_a_suffixed_id_needs_no_effort_flag(self):
        import agy_cli
        assert agy_cli._effort_args("gemini-3.7-flash-high", "") == []
        assert agy_cli._effort_args("gemini-3.7-flash-high", "low") == []

    def test_a_bare_id_gets_an_explicit_effort_flag(self):
        import agy_cli
        assert agy_cli._effort_args("gemini-3.7-flash", "medium") == \
            ["--effort", "medium"]

    def test_a_bare_id_with_no_effort_fails_with_a_readable_message(self):
        """The CLI's own error ("--model X requires --effort") reads like a model
        problem when it is a configuration one — and that cost the first live
        call."""
        import agy_cli
        with pytest.raises(claude_cli.ClaudeError, match="no effort suffix"):
            agy_cli._effort_args("gemini-3.7-flash", "")

    def test_a_pinned_bare_gemini_model_still_carries_an_effort(self, monkeypatch):
        """PRIMARY_MODEL is the instant rollback. It broke on the agy branch,
        which dropped the effort and produced the exact CLI error above."""
        seen = {}
        monkeypatch.setattr(llm_cli, "PRIMARY_MODEL", "gemini-3.7-flash")
        monkeypatch.setattr(llm_cli, "PRIMARY_EFFORT", "high")

        def agy(prompt, *, model, effort, **k):
            seen.update(model=model, effort=effort)
            return {"kind": "meal"}

        monkeypatch.setattr(llm_cli.agy_cli, "call_agy_json", agy)
        llm_cli.call_json("p", timeout_s=10, source="coach")
        assert seen == {"model": "gemini-3.7-flash", "effort": "high"}
