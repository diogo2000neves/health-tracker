"""Unit tests for the local Claude meal estimator and its Gemini fallback.

Two properties matter more than the rest:

* **The cloud deployment is untouched.** With `MEAL_ESTIMATOR` unset, nothing in
  this path may run — that is what lets Cloud Run and the laptop serve the same
  code during the parallel run.
* **Claude failing is not a meal failing.** A spent 5-hour usage window is an
  expected daily event on a subscription, so every failure mode has to fall through
  to Gemini rather than raise. The queue's insertion guarantee is worth nothing if
  the estimator can break it.
"""
import importlib.util
import pathlib
import sys

import pytest

_INGEST = pathlib.Path(__file__).resolve().parent.parent / "ingest"
if str(_INGEST) not in sys.path:
    sys.path.insert(0, str(_INGEST))

import claude_estimator  # noqa: E402

_PATH = _INGEST / "main.py"
_spec = importlib.util.spec_from_file_location("ingest_main_claude", _PATH)
ingest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("MEAL_ESTIMATOR", "claude")


# -- the gate ------------------------------------------------------------------
def test_disabled_by_default_so_cloud_run_is_unaffected(monkeypatch):
    monkeypatch.delenv("MEAL_ESTIMATOR", raising=False)
    assert claude_estimator.enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("claude", True), ("CLAUDE", True), (" claude ", True),
    ("gemini", False), ("", False),
])
def test_enabled_reads_the_switch(monkeypatch, value, expected):
    monkeypatch.setenv("MEAL_ESTIMATOR", value)
    assert claude_estimator.enabled() is expected


def test_try_claude_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("MEAL_ESTIMATOR", raising=False)
    called = []
    monkeypatch.setattr(claude_estimator, "analyze",
                        lambda *a, **k: called.append(1))
    assert ingest._try_claude("prompt") is None
    assert called == [], "the estimator must not be invoked when disabled"


# -- model / effort selection --------------------------------------------------
def test_defaults_are_sonnet_5_high():
    assert claude_estimator.DEFAULT_MODEL == "claude-sonnet-5"
    assert claude_estimator.DEFAULT_EFFORT == "high"


def test_model_and_effort_are_env_overridable(monkeypatch):
    monkeypatch.setenv("CLAUDE_MEAL_MODEL", "claude-opus-5")
    monkeypatch.setenv("CLAUDE_MEAL_EFFORT", "low")
    assert claude_estimator.model() == "claude-opus-5"
    assert claude_estimator.effort() == "low"


def test_timeout_survives_a_junk_env_value(monkeypatch):
    """A typo in the env file must not make every meal fail; fall back to the
    default rather than raising ValueError on each analysis."""
    monkeypatch.setenv("CLAUDE_MEAL_TIMEOUT_S", "not-a-number")
    assert claude_estimator.timeout_s() == claude_estimator.DEFAULT_TIMEOUT_S


def test_timeout_is_generous_enough_for_a_complex_plate():
    """High effort measures 6.5-9 min on a complex plate. A short timeout would
    discard a good answer on exactly the meals Claude is most worth having for."""
    assert claude_estimator.DEFAULT_TIMEOUT_S >= 540


# -- the prompt contract -------------------------------------------------------
def test_json_instructions_demand_reasoning_before_the_numbers():
    """Gemini gets this ordering from RESPONSE_SCHEMA.property_ordering, and the
    docs call it the main accuracy lever. The CLI has no schema, so the prompt is
    the only thing enforcing it."""
    block = claude_estimator.JSON_INSTRUCTIONS
    assert block.index('"reasoning"') < block.index('"items"')
    assert block.index('"reasoning"') < block.index('"confidence"')


def test_json_instructions_cover_every_key_the_record_needs():
    block = claude_estimator.JSON_INSTRUCTIONS
    for key in ("kind", "body", "meal_time", "template", "template_scale",
                "save_template_name", "items", "confidence"):
        assert f'"{key}"' in block, f"{key} missing from the output contract"


def test_json_instructions_pin_the_per_item_shape():
    """Gemini gets the item shape from RESPONSE_SCHEMA's nested `items` schema,
    which Claude never sees. The first live run omitted `portion_g` entirely and
    every item came back 0 g — so the item contract has to be stated here, and
    `nutrients` with it or the day's 15 micronutrient totals go blank.
    """
    block = claude_estimator.JSON_INSTRUCTIONS
    for key in ("name", "name_pt", "cooking_method", "portion_g", "calories",
                "protein_g", "carbs_g", "fat_g", "nutrients"):
        assert f'"{key}"' in block, f"item key {key} missing from the contract"


def test_required_item_keys_match_the_gemini_schema():
    """Drift here is silent: Gemini would keep filling a field Claude had stopped
    being asked for, and only some meals would carry it."""
    block = claude_estimator.JSON_INSTRUCTIONS
    required = ingest.RESPONSE_SCHEMA.properties["items"].items.required
    for key in required:
        line = next((l for l in block.splitlines() if f'"{key}"' in l), "")
        assert "REQUIRED" in line, f"{key} is required by the schema but not here"


# -- transport -----------------------------------------------------------------
class _FakeCLI:
    """Stands in for automation/nutrition-audit/claude_cli.py."""

    def __init__(self, answer=None, raises=None):
        self.answer = answer or {"kind": "meal", "items": [], "confidence": 0.5}
        self.raises = raises
        self.calls = []

    def call_claude_json(self, prompt, *, model, effort, timeout_s, require_key,
                         tools):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort,
                           "timeout_s": timeout_s, "require_key": require_key,
                           "tools": tools})
        if self.raises:
            raise self.raises
        return dict(self.answer)


def test_requires_kind_not_items(monkeypatch):
    """A scale screenshot legitimately returns no items, so requiring "items"
    would make every weigh-in fail to parse."""
    fake = _FakeCLI()
    monkeypatch.setattr(claude_estimator, "_cli", lambda: fake)
    claude_estimator.analyze("prompt")
    assert fake.calls[0]["require_key"] == "kind"


def test_text_only_call_gets_no_tools(monkeypatch):
    """A prompt that needs no tools must be given none: a model that CAN write a
    file may answer by writing one, which is how the coach's first run through this
    wrapper returned prose instead of JSON."""
    fake = _FakeCLI()
    monkeypatch.setattr(claude_estimator, "_cli", lambda: fake)
    claude_estimator.analyze("prompt")
    assert fake.calls[0]["tools"] == ""


def test_image_call_is_locked_to_read(monkeypatch):
    fake = _FakeCLI()
    monkeypatch.setattr(claude_estimator, "_cli", lambda: fake)
    claude_estimator.analyze("prompt", [(b"\x89PNG-ish", "image/png")])
    assert fake.calls[0]["tools"] == "Read"


def test_images_are_written_named_and_then_cleaned_up(monkeypatch):
    """The CLI opens paths, not bytes, and dispatches on the extension."""
    fake = _FakeCLI()
    monkeypatch.setattr(claude_estimator, "_cli", lambda: fake)
    claude_estimator.analyze("prompt", [(b"jpg-bytes", "image/jpeg"),
                                        (b"png-bytes", "image/png")])
    prompt = fake.calls[0]["prompt"]
    assert "meal_0.jpg" in prompt and "meal_1.png" in prompt

    # The paths named in the prompt must be gone once the call returns.
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("/") and "ht-meal-" in stripped:
            assert not pathlib.Path(stripped).exists(), "temp image leaked"


def test_temp_images_are_cleaned_up_even_when_the_call_fails(monkeypatch):
    leaked = {}

    def capture(prompt, **kw):
        for line in prompt.splitlines():
            s = line.strip()
            if "ht-meal-" in s and s.startswith("/"):
                leaked["dir"] = pathlib.Path(s).parent
        raise RuntimeError("usage limit reached")

    fake = _FakeCLI()
    fake.call_claude_json = capture
    monkeypatch.setattr(claude_estimator, "_cli", lambda: fake)
    with pytest.raises(RuntimeError):
        claude_estimator.analyze("prompt", [(b"x", "image/jpeg")])
    assert leaked and not leaked["dir"].exists()


def test_missing_cli_raises_so_the_caller_falls_back(monkeypatch):
    monkeypatch.setattr(claude_estimator, "_cli", lambda: None)
    with pytest.raises(RuntimeError):
        claude_estimator.analyze("prompt")


# -- integration with the record assembly --------------------------------------
def test_successful_claude_answer_becomes_a_meal_record(monkeypatch):
    monkeypatch.setattr(claude_estimator, "analyze", lambda *a, **k: {
        "kind": "meal", "confidence": 0.8,
        "items": [{"name": "oats", "portion_g": 80, "calories": 300,
                   "protein_g": 10, "carbs_g": 54, "fat_g": 6}],
    })
    record = ingest._try_claude("prompt")
    assert record["kind"] == "meal"
    assert record["model"] == "claude-sonnet-5"
    assert record["calories"] == 300


def test_claude_can_return_a_scale_reading(monkeypatch):
    """The router fork has to work through the Claude path too — the weigh-in is
    what triggers the daily sync, so losing it costs more than a meal."""
    monkeypatch.setattr(claude_estimator, "analyze", lambda *a, **k: {
        "kind": "body",
        "body": {"measured_at": "2026-07-29T07:03", "weight_kg": 78.4},
    })
    record = ingest._try_claude("prompt", allow_body=True)
    assert record["kind"] == "body"
    assert record["body"]["weight_kg"] == 78.4
    assert record["measured_at"] == "2026-07-29T07:03"


def test_body_verdict_is_refused_on_a_text_note(monkeypatch):
    """A text note has no screen to OCR, so a "body" verdict is a hallucination and
    must degrade to a meal — same rule the Gemini path enforces."""
    monkeypatch.setattr(claude_estimator, "analyze", lambda *a, **k: {
        "kind": "body", "body": {"weight_kg": 78.4}, "items": [],
    })
    record = ingest._try_claude("prompt", allow_body=False, allow_bowel=True)
    assert record["kind"] == "meal"


def test_implausible_scale_reading_is_still_dropped(monkeypatch):
    """`_normalize_body`'s plausibility bands are the guard against a confident
    misread. Routing Claude through the SAME _record_from is what keeps them
    applied — a parallel implementation would have been a way to lose them."""
    monkeypatch.setattr(claude_estimator, "analyze", lambda *a, **k: {
        "kind": "body", "body": {"weight_kg": 9999},
    })
    record = ingest._try_claude("prompt", allow_body=True)
    assert "weight_kg" not in record["body"]


# -- the fallback --------------------------------------------------------------
def test_spent_usage_window_falls_through_to_gemini(monkeypatch):
    def spent(*a, **k):
        raise RuntimeError("claude reported error: usage limit reached")

    monkeypatch.setattr(claude_estimator, "analyze", spent)
    assert ingest._try_claude("prompt") is None


def test_unparseable_answer_falls_through_to_gemini(monkeypatch):
    def garbage(*a, **k):
        raise ValueError("no 'kind'-bearing JSON object in claude output")

    monkeypatch.setattr(claude_estimator, "analyze", garbage)
    assert ingest._try_claude("prompt") is None


def test_answer_that_breaks_record_assembly_falls_through(monkeypatch):
    """Claude answering with something structurally wrong must cost the meal its
    Claude estimate, not the row."""
    monkeypatch.setattr(claude_estimator, "analyze",
                        lambda *a, **k: {"kind": "meal", "items": "not-a-list"})
    record = ingest._try_claude("prompt")
    # Either a degraded-but-valid record or a clean fallback; never an exception.
    assert record is None or record["kind"] == "meal"
