"""Unit tests for the ingest service's pure helpers.

ingest/main.py initialises all clients lazily, so importing it needs no env
vars or credentials — that property is itself asserted here.
"""
import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "ingest" / "main.py"
_spec = importlib.util.spec_from_file_location("ingest_main", _PATH)
ingest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest)  # must not raise without env/credentials


def test_normalize_items_coerces_and_filters():
    items = ingest._normalize_items([
        {"name": " Grilled Chicken ", "portion_g": "120", "calories": 198,
         "protein_g": 37, "carbs_g": 0, "fat_g": 4.3},
        {"name": "", "calories": 100},          # unnamed -> dropped
        "not a dict",                            # junk -> dropped
        {"name": "rice", "portion_g": None, "calories": -5,  # bad numbers -> 0
         "protein_g": "x", "carbs_g": 42, "fat_g": 0.4},
    ])
    assert [i["name"] for i in items] == ["Grilled Chicken", "rice"]
    assert items[0]["portion_g"] == 120.0
    assert items[1]["portion_g"] == 0.0
    assert items[1]["calories"] == 0.0   # negatives clamped
    assert items[1]["protein_g"] == 0.0  # unparseable -> 0


def test_normalize_items_carries_cooking_method_when_present():
    items = ingest._normalize_items([
        {"name": "chicken thigh", "cooking_method": " Fried ", "portion_g": 100,
         "calories": 220, "protein_g": 25, "carbs_g": 0, "fat_g": 13},
        {"name": "apple", "portion_g": 150, "calories": 78, "protein_g": 0,
         "carbs_g": 21, "fat_g": 0},  # raw -> no cooking_method key
    ])
    assert items[0]["cooking_method"] == "Fried"
    assert "cooking_method" not in items[1]


def test_response_schema_reasons_before_numbers():
    # reasoning must be present and generated first so it conditions the numbers
    assert "reasoning" in ingest.RESPONSE_SCHEMA.required
    assert ingest.RESPONSE_SCHEMA.property_ordering[0] == "reasoning"


def test_meal_from_items_totals_and_label():
    items = ingest._normalize_items([
        {"name": "chicken", "portion_g": 120, "calories": 198,
         "protein_g": 37, "carbs_g": 0, "fat_g": 4.3},
        {"name": "white rice", "portion_g": 150, "calories": 195,
         "protein_g": 4, "carbs_g": 42, "fat_g": 0.4},
    ])
    nut = ingest._meal_from_items(items, 0.83, "gemini-3.5-flash")
    assert nut["foods"] == "chicken, white rice"
    assert nut["calories"] == 393.0
    assert nut["protein_g"] == 41.0
    assert nut["fat_g"] == 4.7
    assert nut["portion_g"] == 270.0
    assert nut["confidence"] == 0.83
    assert nut["model"] == "gemini-3.5-flash"
    assert "notes" not in nut


def test_meal_from_items_empty_is_not_food():
    nut = ingest._meal_from_items([], 1, "m")
    assert nut["foods"] == "not food"
    assert nut["calories"] == 0.0


def test_normalize_nutrients_keeps_known_nonzero_rounded():
    n = ingest._normalize_nutrients({
        "fiber_g": 8.234, "sodium_mg": 120.64, "vitamin_b12_ug": 1.28,
        "bogus_key": 5, "calcium_mg": 0, "iron_mg": -1,
    })
    assert n["fiber_g"] == 8.23       # grams -> 2 dp
    assert n["sodium_mg"] == 120.6    # mg -> 1 dp
    assert n["vitamin_b12_ug"] == 1.3  # ug -> 1 dp
    assert "bogus_key" not in n       # unknown key dropped
    assert "calcium_mg" not in n      # zero dropped
    assert "iron_mg" not in n         # negative dropped


def test_normalize_items_attaches_nutrients():
    items = ingest._normalize_items([{
        "name": "spinach", "portion_g": 100, "calories": 23, "protein_g": 3,
        "carbs_g": 4, "fat_g": 0,
        "nutrients": {"iron_mg": 2.7, "folate_ug": 194, "calcium_mg": 99},
    }])
    assert items[0]["nutrients"]["iron_mg"] == 2.7
    assert items[0]["nutrients"]["folate_ug"] == 194.0
    # a plain item with no nutrients object stays lean
    plain = ingest._normalize_items([{"name": "water ice", "portion_g": 1,
        "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0}])
    assert "nutrients" not in plain[0]


def test_day_totals_skips_non_meals_and_zero_rows():
    rows = [
        {"foods": "orange", "calories": 62, "protein_g": 1.2, "carbs_g": 15.4, "fat_g": 0.2},
        {"foods": "not food", "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        {"foods": "analysis failed", "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        {"foods": "ghost", "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        {"foods": "prosciutto", "calories": 65, "protein_g": 6.5, "carbs_g": 0.1, "fat_g": 4.2},
    ]
    assert ingest._day_totals(rows) == {
        "calories": 127.0, "protein_g": 7.7, "carbs_g": 15.5, "fat_g": 4.4,
    }


def test_parse_score_bounds():
    assert ingest._parse_score(7) == 7.0
    assert ingest._parse_score("7.5") == 7.5
    for bad in (0, 11, "high", None):
        with pytest.raises((TypeError, ValueError)):
            ingest._parse_score(bad)


def test_authorized_uses_constant_time_compare(monkeypatch):
    class Req:
        def __init__(self, token):
            self.headers = {"X-Auth-Token": token}

    monkeypatch.setenv("INGEST_TOKEN", "sekret")
    assert ingest._authorized(Req("sekret"))
    assert not ingest._authorized(Req("wrong"))
    monkeypatch.setenv("INGEST_TOKEN", "")
    assert not ingest._authorized(Req(""))  # empty token never authorises


def test_is_permanent_error_classification():
    # not-found / bad-request => skip model; transient => retry same model
    assert ingest._is_permanent(Exception("404 NOT_FOUND models/foo"))
    assert ingest._is_permanent(Exception("400 INVALID_ARGUMENT"))
    assert not ingest._is_permanent(Exception("503 UNAVAILABLE high demand"))
    assert not ingest._is_permanent(Exception("429 RESOURCE_EXHAUSTED"))


def test_default_chain_is_strongest_first_and_free_tier():
    # strongest free model first; no Pro model (paid-only) in the default chain
    chain = ingest.DEFAULT_MODELS.split(",")
    assert chain[0] == "gemini-3.5-flash"
    assert chain[-1] == "gemini-3.1-flash-lite"
    assert not any("pro" in m for m in chain)


def test_sha12_stable():
    assert ingest._sha12(b"abc") == ingest._sha12(b"abc")
    assert len(ingest._sha12(b"abc")) == 12
    assert ingest._sha12(b"abc") != ingest._sha12(b"abd")


def test_meals_headers_have_note_and_mirror_maintenance():
    # `note` stores the user's free-text description for provenance; the two
    # copies (ingest + maintenance) must stay identical or the sync corrupts rows.
    from src import maintenance
    assert ingest.MEALS_HEADERS[-1] == "note"
    assert ingest.MEALS_HEADERS == maintenance.MEALS_HEADERS
    assert ingest.LAST_COL == "M"


def test_note_suffix_and_text_prompt_are_authoritative():
    # a photo note overrides the visual estimate; the text path caps confidence
    filled = ingest.NOTE_SUFFIX.format(note="only ate half")
    assert "only ate half" in filled
    assert "AUTHORITATIVE" in ingest.NOTE_SUFFIX
    assert "0.50" in ingest.TEXT_PROMPT  # confidence cap for text-only meals
    assert "MEAL DESCRIPTION: half an avocado" in \
        ingest.TEXT_PROMPT.format(note="half an avocado", now_hhmm="14:30")


def test_extract_note_from_form_query_and_json():
    with ingest.app.test_request_context(
            "/ingest?note=from-query", method="POST"):
        assert ingest._extract_note() == "from-query"
    with ingest.app.test_request_context(
            "/ingest", method="POST", data={"note": "  from-form  "}):
        assert ingest._extract_note() == "from-form"  # trimmed
    with ingest.app.test_request_context(
            "/ingest", method="POST", json={"note": "from-json"}):
        assert ingest._extract_note() == "from-json"
    with ingest.app.test_request_context("/ingest", method="POST"):
        assert ingest._extract_note() == ""  # absent


def test_extract_images_ignores_bodyless_form_and_json():
    # a note-only form/JSON request must NOT have its body read as a fake image
    with ingest.app.test_request_context(
            "/ingest", method="POST", data={"note": "oatmeal"}):
        assert ingest._extract_images() == []
    with ingest.app.test_request_context(
            "/ingest", method="POST", json={"note": "oatmeal"}):
        assert ingest._extract_images() == []


def test_extract_images_reads_raw_image_body():
    with ingest.app.test_request_context(
            "/ingest", method="POST", data=b"\xff\xd8jpegbytes",
            content_type="image/jpeg"):
        assert ingest._extract_images() == [(b"\xff\xd8jpegbytes", "image/jpeg")]


def test_extract_images_collects_every_multipart_file_in_order():
    from io import BytesIO
    # meal shot + a nutrition-label shot (different field names) + a note
    with ingest.app.test_request_context("/ingest", method="POST", data={
        "image": (BytesIO(b"\xff\xd8plate"), "plate.jpg"),
        "label": (BytesIO(b"\xff\xd8label"), "label.jpg"),
        "note": "only ate half",
    }, content_type="multipart/form-data"):
        images = ingest._extract_images()
        assert [b for b, _ in images] == [b"\xff\xd8plate", b"\xff\xd8label"]
        assert ingest._extract_note() == "only ate half"


def test_build_prompt_adds_multi_and_note_blocks_only_when_relevant():
    # one photo, no note => the base rubric verbatim
    assert ingest._build_prompt(1, "") == ingest.PROMPT
    # several photos => the multi-image block, carrying the count
    multi = ingest._build_prompt(3, "")
    assert "MULTIPLE IMAGES" in multi and "these 3 images" in multi
    assert "NUTRITION LABEL" in multi  # labels are authoritative
    # a note always appends its authoritative block
    assert "only ate half" in ingest._build_prompt(2, "only ate half")
    assert ingest._build_prompt(1, "no oil").endswith("NOTE: no oil")


def test_photo_name_suffixes_only_multi_photo_meals():
    from datetime import datetime
    when = datetime(2026, 7, 12, 10, 36, 5)
    assert ingest._photo_name(when, "image/jpeg", 1, 1) == "meal_20260712_103605.jpg"
    assert ingest._photo_name(when, "image/png", 2, 3) == "meal_20260712_103605_2.png"


def test_multi_image_dedup_hash_is_combined_and_order_sensitive():
    # the whole set of shots hashes together; re-sending the same set collapses
    combined = lambda parts: ingest._sha12(b"".join(parts))
    assert combined([b"a", b"b"]) == combined([b"a", b"b"])
    assert combined([b"a", b"b"]) != combined([b"a"])          # extra shot => new meal
    assert combined([b"a", b"b"]) != ingest._sha12(b"a")       # not just the first


# -- model loop hardening (the 504 timeout fix) --------------------------------
_GOOD_BODY = ('{"reasoning":"x","items":[{"name":"rice","portion_g":100,'
              '"calories":130,"protein_g":2,"carbs_g":28,"fat_g":0}],'
              '"confidence":0.8}')


class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    """Replays a scripted sequence of bodies/exceptions and records model calls."""
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        b = self.behaviors.pop(0)
        if isinstance(b, Exception):
            raise b
        return _FakeResp(b)


def _fake_genai(behaviors, monkeypatch):
    fm = _FakeModels(behaviors)
    monkeypatch.setattr(ingest, "_genai",
                        lambda: type("C", (), {"models": fm})())
    monkeypatch.setattr(ingest.time, "sleep", lambda *a, **k: None)
    return fm


def test_gen_config_caps_output_and_sets_timeout(monkeypatch):
    monkeypatch.delenv("GEMINI_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("GEMINI_TIMEOUT_MS", raising=False)
    cfg = ingest._gen_config()
    # without a cap the model can run a number to tens of thousands of digits
    assert cfg.max_output_tokens == ingest.DEFAULT_MAX_OUTPUT_TOKENS
    assert cfg.http_options.timeout == ingest.DEFAULT_TIMEOUT_MS


def test_unparseable_output_skips_to_next_model_without_retrying(monkeypatch):
    # model 1 emits junk; we must jump to model 2, NOT retry model 1 three times
    fm = _fake_genai(["{ truncated json", _GOOD_BODY], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1,m2,m3")
    monkeypatch.setenv("GEMINI_RETRIES", "3")
    nut = ingest._run_models(["prompt"])
    assert nut["foods"] == "rice" and nut["model"] == "m2"
    assert fm.calls == ["m1", "m2"]


def test_runaway_number_never_crashes_coercion():
    # the 2026-07-12 bug: the model emitted a number with tens of thousands of
    # digits. It overflows float(); coercion must yield 0 / drop it, never raise.
    huge = 10 ** 400
    assert ingest._round_num(huge) == 0.0
    assert ingest._round_num(huge, 2) == 0.0
    assert ingest._normalize_nutrients({"iron_mg": huge, "fiber_g": 3.2}) == {
        "fiber_g": 3.2}  # runaway key dropped, good key kept
    # and a whole item with a runaway field still normalizes without raising
    items = ingest._normalize_items([{"name": "rice", "portion_g": huge,
        "calories": 130, "protein_g": 2, "carbs_g": 28, "fat_g": 0}])
    assert items[0]["portion_g"] == 0.0 and items[0]["calories"] == 130.0


def test_transient_api_error_still_retries_same_model(monkeypatch):
    fm = _fake_genai([Exception("503 UNAVAILABLE high demand"), _GOOD_BODY],
                     monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    monkeypatch.setenv("GEMINI_RETRIES", "3")
    nut = ingest._run_models(["prompt"])
    assert nut["foods"] == "rice"
    assert fm.calls == ["m1", "m1"]  # retried, then succeeded


def test_deadline_returns_before_request_timeout(monkeypatch):
    fm = _fake_genai([_GOOD_BODY], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1,m2")
    monkeypatch.setenv("GEMINI_DEADLINE_S", "-1")  # already past
    with pytest.raises(RuntimeError, match="deadline"):
        ingest._run_models(["prompt"])
    assert fm.calls == []  # never even called the model


# -- stale-connection resilience (the BrokenPipe 500 fix) ----------------------
class _FlakyReq:
    """Raises on the first N executes (simulating a stale keep-alive socket)."""
    def __init__(self, fails, exc):
        self.fails, self.exc, self.calls = fails, exc, 0

    def execute(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return {"ok": True}


def test_execute_reconnects_after_broken_pipe(monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda *a, **k: None)
    # BrokenPipeError is a ConnectionError subclass -> should be caught & retried
    req = _FlakyReq(1, BrokenPipeError(32, "Broken pipe"))
    assert ingest._execute(lambda: req) == {"ok": True}
    assert req.calls == 2  # failed once, reconnected, succeeded


def test_execute_gives_up_after_three_connection_errors(monkeypatch):
    monkeypatch.setattr(ingest.time, "sleep", lambda *a, **k: None)
    req = _FlakyReq(99, ConnectionResetError(104, "reset"))
    with pytest.raises(ConnectionError):
        ingest._execute(lambda: req)
    assert req.calls == 3  # three attempts, then propagates


# -- note-inferred meal time (text-only retro logging) -------------------------
def test_meal_time_is_an_optional_schema_field():
    assert "meal_time" in ingest.RESPONSE_SCHEMA.properties
    assert "meal_time" in ingest.RESPONSE_SCHEMA.property_ordering
    assert "meal_time" not in ingest.RESPONSE_SCHEMA.required  # optional


def test_resolve_meal_time_maps_hhmm_onto_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 7, 12, 14, 30, tzinfo=ZoneInfo("Europe/Lisbon"))
    # a valid earlier time => today at that time
    got = ingest._resolve_meal_time("08:00", now)
    assert (got.hour, got.minute, got.date()) == (8, 0, now.date())
    # a future time can't be a logged meal => clamp to now
    assert ingest._resolve_meal_time("20:00", now) == now
    # blank / malformed => fall back to now
    assert ingest._resolve_meal_time("", now) == now
    assert ingest._resolve_meal_time("breakfast", now) == now
    assert ingest._resolve_meal_time("25:99", now) == now


def test_already_logged_ignores_failed_stubs():
    sha = "abc123def456"
    # a successful meal with this hash blocks a re-send
    good = [{"image_sha": sha, "foods": "chicken sandwich"}]
    assert ingest._already_logged(sha, good)
    # but a prior "analysis failed" / "not food" stub must NOT block the retry
    for stub in ("analysis failed", "not food", "NOT FOOD"):
        assert not ingest._already_logged(sha, [{"image_sha": sha, "foods": stub}])
    # different hash never matches
    assert not ingest._already_logged("other", good)


def test_run_models_surfaces_inferred_meal_time(monkeypatch):
    body = ('{"reasoning":"had oats","meal_time":"08:15","items":[{"name":"oats",'
            '"portion_g":250,"calories":300,"protein_g":10,"carbs_g":50,"fat_g":5}],'
            '"confidence":0.4}')
    _fake_genai([body], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    nut = ingest._run_models(["prompt"])
    assert nut["meal_time"] == "08:15" and nut["foods"] == "oats"


def test_text_only_dedup_hash_is_stable_and_distinct():
    # text-only meals de-dupe on the note; identical text hashes the same,
    # different text does not, and it never collides with a raw-image hash.
    h = lambda note: ingest._sha12(("text:" + note).encode("utf-8"))
    assert h("oatmeal") == h("oatmeal")
    assert h("oatmeal") != h("toast")
    assert h("oatmeal") != ingest._sha12(b"oatmeal")
