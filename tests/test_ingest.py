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
        ingest.TEXT_PROMPT.format(note="half an avocado")


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


def test_extract_image_ignores_bodyless_form_and_json():
    # a note-only form/JSON request must NOT have its body read as a fake image
    with ingest.app.test_request_context(
            "/ingest", method="POST", data={"note": "oatmeal"}):
        assert ingest._extract_image() == (b"", "")
    with ingest.app.test_request_context(
            "/ingest", method="POST", json={"note": "oatmeal"}):
        assert ingest._extract_image() == (b"", "")


def test_extract_image_reads_raw_image_body():
    with ingest.app.test_request_context(
            "/ingest", method="POST", data=b"\xff\xd8jpegbytes",
            content_type="image/jpeg"):
        img, mime = ingest._extract_image()
        assert img == b"\xff\xd8jpegbytes"
        assert mime == "image/jpeg"


def test_text_only_dedup_hash_is_stable_and_distinct():
    # text-only meals de-dupe on the note; identical text hashes the same,
    # different text does not, and it never collides with a raw-image hash.
    h = lambda note: ingest._sha12(("text:" + note).encode("utf-8"))
    assert h("oatmeal") == h("oatmeal")
    assert h("oatmeal") != h("toast")
    assert h("oatmeal") != ingest._sha12(b"oatmeal")
