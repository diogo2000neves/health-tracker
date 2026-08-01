"""The whole non-food path, through the real endpoint.

The unit tests prove each piece works. This proves they are actually wired: that a
generation reads `daily_summary`, produces sleep/training findings, evaluates links,
and puts all of it into the prompt the model is handed — and, in the other
direction, that a phone-only user reaches the model with none of it and is told so.

That second half is the friend-onboarding guarantee. It is checked here rather than
in a unit test because the failure it guards against is a wiring failure: every
individual gate could be correct while some caller reads the sheet anyway.
"""
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_INGEST = _ROOT / "ingest"
if str(_INGEST) not in sys.path:
    sys.path.insert(0, str(_INGEST))

_spec = importlib.util.spec_from_file_location("ingest_main", _INGEST / "main.py")
ingest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest)

TODAY = datetime(2026, 7, 26, 15, 30)


def meals_with_late_dinners(days=100):
    """Every other day carries a heavy late dinner. Paired with the sleep rows
    below, that is a genuine, correctly-lagged effect for the engine to find."""
    rows = []
    for offset in range(days):
        day = (TODAY.date() - timedelta(days=days - offset)).isoformat()
        rows.append({"datetime": f"{day} 08:15:00", "foods": "oats",
                     "calories": 400, "protein_g": 20, "carbs_g": 50, "fat_g": 10,
                     "items": json.dumps([{"name": "oats", "portion_g": 80,
                                           "calories": 400}])})
        late = offset % 2 == 0
        rows.append({"datetime": f"{day} {'22:30' if late else '19:00'}:00",
                     "foods": "beef steak, white rice",
                     "calories": 1100 if late else 600,
                     "protein_g": 45, "carbs_g": 90, "fat_g": 55 if late else 15,
                     "items": json.dumps([{"name": "beef steak", "portion_g": 200,
                                           "calories": 1100 if late else 600}])})
    return rows


def daily_rows(days=100):
    """A tracker's-eye view of the same period: short, broken nights after the late
    dinners, a stretch of no training, and a body composition drifting the wrong way."""
    import random
    rng = random.Random(5)
    out = []
    for offset in range(days):
        day = (TODAY.date() - timedelta(days=days - offset)).isoformat()
        # The night on row N follows the dinner on row N-1.
        after_late = offset > 0 and (offset - 1) % 2 == 0
        out.append({
            "date": day,
            "sleep_mins": (350 if after_late else 460) + rng.uniform(-8, 8),
            "sleep_deep_mins": (52 if after_late else 100) + rng.uniform(-4, 4),
            "sleep_efficiency_pct": (78 if after_late else 94) + rng.uniform(-2, 2),
            "sleep_start": "23:30" if offset % 2 else "23:40",
            "resting_hr_bpm": 55 + rng.uniform(-2, 2),
            "hrv_ms": 60 + rng.uniform(-5, 5),
            "steps": 9000 + rng.uniform(-500, 500),
            "workout_count": 1 if offset < days - 12 and offset % 3 == 0 else 0,
            "workout_types": "strength training"
                             if offset < days - 12 and offset % 3 == 0 else "",
            "workout_mins": 45 if offset < days - 12 and offset % 3 == 0 else 0,
            "total_cals_out": 2600 + rng.uniform(-100, 100),
            "weight_kg": 70.0 - offset * 0.01,
            "body_fat_pct": 21.0 - offset * 0.002,
            "lean_mass_kg": 56.0 - offset * 0.008,
            "total_fiber_g": 20 + rng.uniform(-3, 3),
        })
    return out


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.delenv("COACH_BUCKET", raising=False)
    monkeypatch.setenv("COACH_LOCAL_DIR", str(tmp_path))
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(ingest, "_all_meal_rows", meals_with_late_dinners)
    monkeypatch.setattr(ingest, "_resolved_targets_and_basis",
                        lambda: ({"calories": {"kind": "window", "floor": 1860,
                                               "ceiling": 2200, "unit": "kcal"},
                                  "protein_g": {"kind": "reach", "floor": 136,
                                                "unit": "g"}},
                                 {"weight_kg": 70.0}))
    monkeypatch.setattr(ingest, "_tz", lambda: None)

    real_datetime = ingest.datetime

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return TODAY
    monkeypatch.setattr(ingest, "datetime", Frozen)
    monkeypatch.setattr(ingest._narrator_mod(), "call_gemini",
                        lambda *a, **kw: {"foods": []})
    return monkeypatch


def _set_config(monkeypatch, rows, daily=None):
    """Point both tab reads at fixtures. `config` is a key/value grid; everything
    else the coach touches is daily_summary."""
    grid = [["key", "value", "notes"]] + [[k, v, ""] for k, v in rows]
    days = daily if daily is not None else daily_rows()
    headers = sorted({k for d in days for k in d})
    daily_grid = [headers] + [[d.get(h, "") for h in headers] for d in days]

    def read_tab(tab):
        if tab == ingest.CONFIG_TAB:
            return grid
        if tab == ingest.DAILY_TAB:
            return daily_grid
        return []
    monkeypatch.setattr(ingest, "_read_tab", read_tab)
    ingest._caps_cache.update({"at": 0.0, "value": None})
    monkeypatch.setattr(ingest, "_seed_config", lambda grid: None)


class TestTheOwnersCoach:
    def test_a_generation_carries_every_domain_into_the_prompt(self, wired):
        _set_config(wired, [("blocks", "full")])
        job = ingest._build_generation_job(TODAY, slot="morning", reason="test")
        assert job is not None
        prompt = job["prompt"]

        # The tracker's numbers reached the model at all.
        assert "body_and_activity" in prompt
        # ...and with the specialist expertise for whatever the findings were about.
        assert "ALIMENTAÇÃO" in prompt
        assert any(frame in prompt for frame in
                   ("SONO E RECUPERAÇÃO", "ATIVIDADE E TREINO",
                    "COMPOSIÇÃO CORPORAL", "AS LIGAÇÕES"))
        # Full capabilities: nothing is declared off-limits.
        assert "O QUE NÃO VÊS" not in prompt

    def test_the_findings_reach_the_job_context_for_validation(self, wired):
        _set_config(wired, [("blocks", "full")])
        job = ingest._build_generation_job(TODAY, slot="morning", reason="test")
        findings = job["context"]["findings"]
        assert findings, "expected at least one finding from a hundred days"
        # Assembly validates the answer against these exact objects, so a card
        # referencing anything else is dropped.
        assert all("id" in f for f in findings)

    def test_a_measured_link_is_found_end_to_end(self, wired):
        _set_config(wired, [("blocks", "full")])
        caps = ingest._capabilities()
        days = ingest._coach_window_days(TODAY)
        meals = ingest._meals_by_waking_day(meals_with_late_dinners())
        found = ingest._coach_metric_findings(days, caps, meals)
        links = [f for f in found if f["domain"] == "link"]
        assert links, "the late-dinner effect should survive the FDR gate"
        for link in links:
            assert link["evidence"]["claim"] == "association"
            assert link["evidence"]["mechanism"]

    def test_the_meal_window_is_bucketed_on_the_waking_day(self, wired):
        # A 00:30 dessert belongs to the evening before. Getting this wrong would
        # compare a feature against the wrong row's nutrition columns.
        assert ingest._waking_day("2026-07-20 00:30:00") == "2026-07-19"
        assert ingest._waking_day("2026-07-20 05:30:00") == "2026-07-20"


class TestTheFriendsCoach:
    CONFIG = [("blocks", "nutrition")]

    def test_no_metric_findings_at_all(self, wired):
        _set_config(wired, self.CONFIG)
        caps = ingest._capabilities()
        assert set(caps.blocks) == {"nutrition", "self_report"}
        days = ingest._coach_window_days(TODAY)
        found = ingest._coach_metric_findings(days, caps, {})
        # Not "silenced" — the sleep rules never ran and no link was evaluable.
        assert [f for f in found if f["domain"] in ("sleep", "activity", "body")] == []
        assert [f for f in found if f["domain"] == "link"] == []

    def test_the_prompt_states_the_blind_spots_and_drops_the_frames(self, wired):
        _set_config(wired, self.CONFIG)
        job = ingest._build_generation_job(TODAY, slot="morning", reason="test")
        assert job is not None
        prompt = job["prompt"]
        assert "O QUE NÃO VÊS" in prompt
        assert "sono e recuperação" in prompt
        assert "composição corporal" in prompt
        # No sleep expertise in a prompt for someone with no sleep data.
        assert "SONO E RECUPERAÇÃO" not in prompt
        assert "COMPOSIÇÃO CORPORAL" not in prompt
        assert "AS LIGAÇÕES" not in prompt

    def test_the_daily_api_never_serves_a_block_they_do_not_have(self, wired):
        _set_config(wired, self.CONFIG)
        client = ingest.app.test_client()
        # Even asking for it by name — an older build would do exactly this.
        body = client.get("/daily?blocks=sleep,body",
                          headers={"X-Auth-Token": "tok"}).get_json()
        assert body["blocks"] == []
        assert body["capabilities"]["blind_spots"]
        for day in body["days"]:
            assert "sleep" not in day and "body" not in day

    def test_an_unknown_block_is_still_a_400(self, wired):
        # Gating must not turn a client bug into a silent empty response.
        _set_config(wired, self.CONFIG)
        client = ingest.app.test_client()
        response = client.get("/daily?blocks=slep",
                              headers={"X-Auth-Token": "tok"})
        assert response.status_code == 400


class TestConfigResilience:
    def test_an_unreadable_config_assumes_full_capabilities(self, wired):
        # Hiding a block the user really has looks like data loss, which is far
        # worse than showing an empty one.
        def explode(tab):
            raise RuntimeError("sheets is down")
        wired.setattr(ingest, "_read_tab", explode)
        ingest._caps_cache.update({"at": 0.0, "value": None})
        assert ingest._capabilities().blocks == \
            ingest.caps_mod.TOGGLEABLE_BLOCKS

    def test_a_missing_config_tab_changes_nothing(self, wired):
        _set_config(wired, [])
        assert ingest._capabilities().blocks == \
            ingest.caps_mod.TOGGLEABLE_BLOCKS
