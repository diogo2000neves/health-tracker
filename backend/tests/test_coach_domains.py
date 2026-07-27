"""How the multi-domain coach decides what to say, and what it is told to say it with.

The risk this whole layer is designed against is the one the original nutrient-first
prompt already demonstrated once: give a model a wider payload and it writes the
average of it, three shallow things instead of one good one. So the checks here are
mostly about restraint — one domain per generation's worth of findings, a prompt that
carries only the expertise it needs, and a hard refusal to let the model claim
anything the deterministic layer did not measure.
"""
import pathlib
import sys
from datetime import datetime

import pytest

_INGEST = pathlib.Path(__file__).resolve().parent.parent / "ingest"
if str(_INGEST) not in sys.path:
    sys.path.insert(0, str(_INGEST))

import coach_feed as feed        # noqa: E402
import narrator                  # noqa: E402

NOW = datetime(2026, 7, 26, 15, 30)
TODAY = "2026-07-26"


def finding(fid, severity, domain=None, **extra):
    out = {"id": fid, "kind": fid.split(":")[-1], "group": None,
           "severity": severity, "headline": f"headline for {fid}",
           "evidence": {}, "foods": []}
    if domain:
        out["domain"] = domain
    out.update(extra)
    return out


class TestRankingAcrossDomains:
    def test_the_most_severe_finding_wins_whatever_it_is_about(self):
        # The point of ranking every domain in one list: a coach that always leads
        # with food and mentions sleep at the bottom never really changed.
        profile = {"findings": [finding("food:fries", 0.3)]}
        extra = [finding("sleep:short_sleep", 0.9, domain="sleep")]
        got = feed.eligible_findings(profile, {}, today=TODAY, extra=extra)
        assert got[0]["id"] == "sleep:short_sleep"

    def test_food_findings_still_win_when_they_are_the_story(self):
        profile = {"findings": [finding("food:fries", 0.9)]}
        extra = [finding("sleep:short_sleep", 0.2, domain="sleep")]
        got = feed.eligible_findings(profile, {}, today=TODAY, extra=extra)
        assert got[0]["id"] == "food:fries"

    def test_one_domain_cannot_take_every_slot(self):
        # A rough fortnight of sleep easily produces the top four findings. The
        # budget is what keeps the user hearing about food that week too.
        profile = {"findings": [finding("food:fries", 0.5)]}
        extra = [finding(f"sleep:s{i}", 0.9 - i / 100, domain="sleep")
                 for i in range(4)]
        got = feed.eligible_findings(profile, {}, today=TODAY, extra=extra,
                                     limit=3)
        domains = [f.get("domain", "nutrition") for f in got]
        assert domains.count("sleep") == feed.MAX_FINDINGS_PER_DOMAIN
        assert "nutrition" in domains

    def test_a_link_gets_a_much_longer_silence_than_a_pattern(self):
        # "Late dinners cost you deep sleep" does not become more useful by being
        # repeated on Thursday.
        assert feed.LINK_COOLDOWN_DAYS > feed.PATTERN_COOLDOWN_DAYS
        link = finding("link:late_calories", 0.8, domain="link")
        state = {"shown": {"link:late_calories":
                           {"date": "2026-07-16", "severity": 0.8}}}   # 10 days ago
        assert feed.eligible_findings({}, state, today=TODAY, extra=[link]) == []
        # A food pattern from the same day is already free to return.
        food = finding("food:fries", 0.8)
        state = {"shown": {"food:fries": {"date": "2026-07-16", "severity": 0.8}}}
        assert feed.eligible_findings({"findings": [food]}, state, today=TODAY)


class TestWantedCards:
    def _facts(self, extra):
        return feed.build_generation_facts(
            slot="morning", now=NOW, profile={"findings": []},
            today={"meals": []}, nutrients={}, memory={}, state={},
            metric_findings=extra)

    def test_a_link_finding_asks_for_a_link_card(self):
        facts = self._facts([finding("link:x", 0.8, domain="link")])
        assert "link" in facts["wanted_cards"]
        assert "pattern" not in facts["wanted_cards"]

    def test_a_domain_finding_asks_for_a_pattern_card(self):
        facts = self._facts([finding("sleep:short", 0.8, domain="sleep")])
        assert "pattern" in facts["wanted_cards"]
        assert "link" not in facts["wanted_cards"]

    def test_no_findings_asks_for_neither(self):
        facts = self._facts([])
        assert "pattern" not in facts["wanted_cards"]
        assert "link" not in facts["wanted_cards"]


class TestAssemblyRefusals:
    def _assemble(self, answer, findings):
        return feed.assemble(answer, slot="morning", now=NOW,
                             profile={"foods": [], "swaps": {}},
                             today={"meals": []}, findings=findings)

    def test_a_link_card_with_no_finding_behind_it_is_dropped(self):
        # The whole grounding rule: the model may not claim a correlation the
        # engine did not measure.
        cards, _ = self._assemble(
            {"cards": [{"kind": "link", "ref": "link:invented",
                        "title": "Descoberta", "body": "Comer tarde faz mal."}]},
            [])
        assert cards == []

    def test_a_link_card_pointing_at_a_food_finding_is_dropped(self):
        # The two kinds carry different evidence and read differently; crossing
        # them would put a link card's framing on a pattern's facts.
        food = finding("food:fries", 0.5)
        cards, _ = self._assemble(
            {"cards": [{"kind": "link", "ref": "food:fries",
                        "title": "Ligação", "body": "..."}]}, [food])
        assert cards == []

    def test_a_pattern_card_pointing_at_a_link_is_dropped(self):
        link = finding("link:x", 0.5, domain="link")
        cards, _ = self._assemble(
            {"cards": [{"kind": "pattern", "ref": "link:x",
                        "title": "Padrão", "body": "..."}]}, [link])
        assert cards == []

    def test_a_valid_link_card_carries_its_evidence_and_domain(self):
        link = finding("link:late", 0.7, domain="link", group="late_calories",
                       evidence={"cause": "calories_after_21h",
                                 "effect": "sleep_deep_mins",
                                 "effect_delta": -18.0, "mechanism": "porque sim",
                                 "claim": "association"})
        cards, shown = self._assemble(
            {"cards": [{"kind": "link", "ref": "link:late",
                        "title": "Jantares tardios",
                        "body": "Nas noites a seguir..."}]}, [link])
        assert len(cards) == 1
        card = cards[0]
        assert card["kind"] == "link" and card["domain"] == "link"
        assert card["evidence"]["mechanism"] == "porque sim"
        assert card["evidence"]["effect_delta"] == -18.0
        assert "link:late" in shown

    def test_a_sleep_card_never_carries_a_food_swap(self):
        # A sleep finding has no food to trade; offering one would be exactly the
        # untethered advice the swap validation exists to stop.
        sleep = finding("sleep:short", 0.6, domain="sleep")
        cards, _ = self._assemble(
            {"cards": [{"kind": "pattern", "ref": "sleep:short",
                        "title": "Sono", "body": "...",
                        "swap": {"from": "arroz", "to": "quinoa",
                                 "why": "porquê"}}]}, [sleep])
        assert cards[0]["swap"] is None

    def test_a_domain_card_is_filed_under_its_domain(self):
        sleep = finding("sleep:short", 0.6, domain="sleep")
        cards, _ = self._assemble(
            {"cards": [{"kind": "pattern", "ref": "sleep:short",
                        "title": "Sono", "body": "..."}]}, [sleep])
        assert cards[0]["domain"] == "sleep"


class TestPromptComposition:
    def _prompt(self, findings=(), capabilities=None):
        return narrator.build_feed_prompt({
            "findings": list(findings),
            "capabilities": capabilities,
            "wanted_cards": ["day_plan"],
        })

    def test_a_food_only_day_gets_no_other_specialist_frames(self):
        # The dilution guard: on a day whose findings are all about food, the
        # prompt is the one the coach had before domains existed.
        prompt = self._prompt([finding("food:fries", 0.5)])
        assert "ALIMENTAÇÃO" in prompt
        assert "SONO E RECUPERAÇÃO" not in prompt
        assert "COMPOSIÇÃO CORPORAL" not in prompt

    def test_a_sleep_day_gets_sleep_expertise(self):
        prompt = self._prompt([finding("sleep:short", 0.8, domain="sleep")])
        assert "SONO E RECUPERAÇÃO" in prompt
        assert "ALIMENTAÇÃO" in prompt          # food is always in play
        assert "ATIVIDADE E TREINO" not in prompt

    def test_a_link_day_gets_the_association_rules(self):
        prompt = self._prompt([finding("link:x", 0.8, domain="link")])
        assert "AS LIGAÇÕES" in prompt
        assert "nunca como causa" in prompt

    def test_every_frame_names_the_generic_version_it_must_avoid(self):
        # Telling a model what good looks like is weaker than also telling it what
        # the cheap version sounds like, by name.
        for domain, frame in narrator._DOMAIN_FRAMES.items():
            if domain == "link":
                continue
            assert "GENÉRICO A EVITAR" in frame or "NUNCA" in frame, domain

    def test_a_blind_spot_is_stated_in_words(self):
        # Silence is not enough: a model given no sleep data still writes
        # confident sleep advice unless it is told the data does not exist.
        prompt = self._prompt(capabilities={
            "blind_spots": ["sleep", "activity", "body"],
            "goal_label_pt": "comer melhor"})
        assert "O QUE NÃO VÊS" in prompt
        assert "sono e recuperação" in prompt
        assert "composição corporal" in prompt

    def test_a_full_capability_states_no_blind_spots(self):
        prompt = self._prompt(capabilities={"blind_spots": [],
                                            "goal_label_pt": "recomposição"})
        assert "O QUE NÃO VÊS" not in prompt

    def test_the_goal_is_carried_not_hard_coded(self):
        # It used to say "recomposição corporal" for everyone, which is wrong the
        # moment a second person uses the app.
        prompt = self._prompt(capabilities={"blind_spots": [],
                                            "goal_label_pt": "ganho de massa"})
        assert "ganho de massa" in prompt

    def test_the_schema_offers_the_link_kind(self):
        assert "link" in narrator._FEED_SCHEMA
