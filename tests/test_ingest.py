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
    nut = ingest._meal_from_items(items, 0.83, "fork as reference", "m")
    assert nut["foods"] == "chicken, white rice"
    assert nut["calories"] == 393.0
    assert nut["protein_g"] == 41.0
    assert nut["fat_g"] == 4.7
    assert nut["portion_g"] == 270.0
    assert nut["confidence"] == 0.83


def test_meal_from_items_empty_is_not_food():
    nut = ingest._meal_from_items([], 1, "", "m")
    assert nut["foods"] == "not food"
    assert nut["calories"] == 0.0


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


def test_sha12_stable():
    assert ingest._sha12(b"abc") == ingest._sha12(b"abc")
    assert len(ingest._sha12(b"abc")) == 12
    assert ingest._sha12(b"abc") != ingest._sha12(b"abd")
