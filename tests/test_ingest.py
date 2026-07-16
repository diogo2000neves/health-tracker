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


def test_response_schema_classifies_then_reasons_before_numbers():
    order = ingest.RESPONSE_SCHEMA.property_ordering
    # `kind` is decided first — the meal/body fork conditions everything after it.
    assert order[0] == "kind"
    assert "kind" in ingest.RESPONSE_SCHEMA.required
    # then reasoning, still ahead of the numbers it is meant to condition
    assert "reasoning" in ingest.RESPONSE_SCHEMA.required
    assert order.index("reasoning") < order.index("items")
    assert order.index("reasoning") < order.index("confidence")


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


def test_default_chain_is_flash_lite_first_and_free_tier():
    # flash-lite leads (the reliable/fast one; see 2026-07-12/13 incidents); the
    # stronger flash models follow for the worker's thorough pass. No Pro (paid).
    chain = ingest.DEFAULT_MODELS.split(",")
    assert chain[0] == "gemini-3.1-flash-lite"
    assert "gemini-3.5-flash" in chain
    assert not any("pro" in m for m in chain)


def test_quick_kwargs_is_a_single_fast_pass(monkeypatch):
    monkeypatch.setenv("GEMINI_MODELS", "fast-lite,strong-a,strong-b")
    kw = ingest._quick_kwargs()
    assert kw["models"] == ["fast-lite"]   # only the first (fastest) model
    assert kw["retries"] == 1              # one shot, no waiting on retries
    assert kw["timeout_ms"] <= 45000 and kw["deadline_s"] <= 60  # tight budget


def test_run_models_respects_model_override(monkeypatch):
    # the quick pass must call ONLY the model it's given, not the whole chain
    fm = _fake_genai([_GOOD_MEAL], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "a,b,c")
    ingest._run_models(["prompt"], models=["only-this"], retries=1)
    assert fm.calls == ["only-this"]


def test_sha12_stable():
    assert ingest._sha12(b"abc") == ingest._sha12(b"abc")
    assert len(ingest._sha12(b"abc")) == 12
    assert ingest._sha12(b"abc") != ingest._sha12(b"abd")


# -- measured meal templates ---------------------------------------------------
_TPL = [{
    "name": "Sandes mista PA",
    "description": "baguette, ham, cheese, butter",
    "items": ingest._normalize_items([
        {"name": "baguette", "portion_g": 80, "calories": 200,
         "protein_g": 7, "carbs_g": 40, "fat_g": 1,
         "nutrients": {"fiber_g": 2.0, "sodium_mg": 300}},
        {"name": "ham", "portion_g": 40, "calories": 60,
         "protein_g": 8, "carbs_g": 1, "fat_g": 3},
    ]),
}]


def test_apply_template_swaps_estimate_for_measured_values():
    est = ingest._meal_from_items(ingest._normalize_items(
        [{"name": "sandwich", "portion_g": 150, "calories": 400,
          "protein_g": 20, "carbs_g": 45, "fat_g": 12}]), 0.6, "m1")
    est["template"], est["template_scale"] = "Sandes mista PA", 1
    out = ingest.apply_template(est, _TPL)
    assert out["calories"] == 260.0            # 200 + 60, the MEASURED values
    assert out["portion_g"] == 120.0           # 80 + 40
    assert out["confidence"] == ingest.TEMPLATE_CONFIDENCE   # measured, not guessed
    assert out["template"] == "Sandes mista PA"
    assert out["model"] == "m1"                # which model matched (audit)


def test_apply_template_scales_when_only_part_eaten():
    est = ingest._meal_from_items([], 0.5, "m1")
    est["template"], est["template_scale"] = "sandes  mista pa", 0.5  # loose name
    out = ingest.apply_template(est, _TPL)
    assert out["calories"] == 130.0            # half of 260
    assert out["portion_g"] == 60.0
    assert out["template"] == "Sandes mista PA (x0.5)"
    assert out["items"][0]["nutrients"]["fiber_g"] == 1.0   # nutrients scale too


def test_apply_template_ignores_a_hallucinated_name():
    est = ingest._meal_from_items(ingest._normalize_items(
        [{"name": "rice", "portion_g": 100, "calories": 130,
          "protein_g": 3, "carbs_g": 28, "fat_g": 0}]), 0.7, "m1")
    est["template"] = "A Template That Does Not Exist"
    out = ingest.apply_template(est, _TPL)
    assert out["calories"] == 130.0            # the ESTIMATE is kept
    assert out["template"] == ""               # and the bogus name is dropped


def test_apply_template_is_a_noop_without_a_match():
    est = ingest._meal_from_items(ingest._normalize_items(
        [{"name": "rice", "portion_g": 100, "calories": 130,
          "protein_g": 3, "carbs_g": 28, "fat_g": 0}]), 0.7, "m1")
    est["template"] = ""
    assert ingest.apply_template(est, _TPL) is est


def test_forced_template_honours_a_note_that_names_one():
    # naming a template in the note is an instruction, not a hint — deterministic
    assert ingest._forced_template(
        "usa o template Sandes mista PA", _TPL)["name"] == "Sandes mista PA"
    assert ingest._forced_template(
        "this is my template  sandes   MISTA pa", _TPL)["name"] == "Sandes mista PA"
    # the word "template" alone isn't enough — a name must be present
    assert ingest._forced_template("we have a template for this", _TPL) is None
    # and the name alone, without the word "template", doesn't force it
    assert ingest._forced_template("sandes mista pa", _TPL) is None
    assert ingest._forced_template("", _TPL) is None


def test_forced_template_prefers_the_longest_matching_name():
    tpls = _TPL + [{"name": "Sandes mista PA com ovo", "description": "",
                    "items": _TPL[0]["items"]}]
    got = ingest._forced_template("template Sandes mista PA com ovo hoje", tpls)
    assert got["name"] == "Sandes mista PA com ovo"   # not shadowed by the shorter


def test_resolve_templates_forces_the_named_one_over_the_models_guess(monkeypatch):
    from datetime import datetime
    est = ingest._meal_from_items(ingest._normalize_items(
        [{"name": "sandwich", "portion_g": 150, "calories": 400,
          "protein_g": 20, "carbs_g": 45, "fat_g": 12}]), 0.6, "m1")
    est["template"] = ""            # the model FAILED to recognise it
    est["save_template_name"] = ""
    out = ingest._resolve_templates(
        est, "usa o template Sandes mista PA", datetime.now(), _TPL)
    assert out["template"] == "Sandes mista PA"
    assert out["calories"] == 260.0                  # measured values won anyway
    assert out["confidence"] == ingest.TEMPLATE_CONFIDENCE


def test_template_catalogue_lists_measured_ingredients():
    cat = ingest._template_catalogue(_TPL)
    assert '"Sandes mista PA"' in cat and "baguette 80g" in cat and "ham 40g" in cat
    assert "260 kcal" in cat


def test_maybe_save_template_requires_the_note_to_mention_one(monkeypatch):
    saved = {}
    monkeypatch.setattr(ingest, "save_template",
                        lambda n, nut, when: saved.update(name=n))
    from datetime import datetime
    nut = ingest._meal_from_items([], 1, "m1")
    nut["save_template_name"] = "Sandes mista PA"
    # the model named one but the note never asked -> refuse to persist
    assert ingest.maybe_save_template(nut, "just a normal meal", datetime.now()) == ""
    assert not saved
    # note genuinely asks -> saved
    assert ingest.maybe_save_template(
        nut, "guarda como template Sandes mista PA", datetime.now()) == "Sandes mista PA"
    assert saved["name"] == "Sandes mista PA"


def test_templates_block_has_match_rules_only_when_templates_exist():
    with_tpl = ingest._templates_block(_TPL)
    assert "KNOWN MEAL TEMPLATES" in with_tpl and "Sandes mista PA" in with_tpl
    assert "SAVING A TEMPLATE" in with_tpl
    # no templates yet: no match rules, but you can still create the first one
    empty = ingest._templates_block([])
    assert "KNOWN MEAL TEMPLATES" not in empty
    assert "SAVING A TEMPLATE" in empty


def test_template_fields_are_optional_in_the_schema():
    props = ingest.RESPONSE_SCHEMA.properties
    for field in ("template", "template_scale", "save_template_name"):
        assert field in props
        assert field not in ingest.RESPONSE_SCHEMA.required


def test_meals_headers_mirror_maintenance():
    # `note` (user's text) and `template` (which measured template supplied the
    # numbers) are provenance columns. The two copies (ingest + maintenance) must
    # stay identical or the schema sync corrupts existing rows.
    from src import maintenance
    assert ingest.MEALS_HEADERS[-2:] == ["note", "template"]
    assert ingest.MEALS_HEADERS == maintenance.MEALS_HEADERS
    assert ingest.LAST_COL == "N"
    # the templates tab schema is mirrored too
    assert ingest.TEMPLATES_HEADERS == maintenance.TEMPLATES_HEADERS


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


def _jpeg(scan=b"\x11\x22", extra_segments=b""):
    # minimal parser-valid JPEG: SOI, optional extra length-delimited segments,
    # SOS (len 3, one header byte), entropy `scan`, EOI
    return b"\xff\xd8" + extra_segments + b"\xff\xda\x00\x03\x00" + scan + b"\xff\xd9"


def test_split_jpegs_separates_concatenated_photos():
    a, b, c = _jpeg(b"\xaa\xaa"), _jpeg(b"\xbb\xbb"), _jpeg(b"\xcc\xcc")
    assert ingest._split_jpegs(a + b + c) == [a, b, c]


def test_split_jpegs_keeps_a_single_photo_with_embedded_thumbnail_intact():
    # a real iPhone JPEG has an EXIF thumbnail (its own FF D8/FF D9) inside APP1;
    # that inner boundary must NOT be treated as a second image
    thumb = b"\xff\xd8\xaa\xbb\xff\xd9"                 # nested thumbnail JPEG
    app1 = b"\xff\xe1" + (2 + len(thumb)).to_bytes(2, "big") + thumb
    one_photo = _jpeg(b"\x11\x22", extra_segments=app1)
    assert one_photo.count(b"\xff\xd8") == 2            # two SOI markers present…
    assert ingest._split_jpegs(one_photo) == [one_photo]  # …but it's ONE image


def test_split_jpegs_passes_through_non_jpeg():
    png = b"\x89PNG\r\n\x1a\n" + b"whatever"
    assert ingest._split_jpegs(png) == [png]


def test_images_from_json_decodes_a_base64_array():
    import base64
    a, b = _jpeg(b"\xaa\xaa"), _jpeg(b"\xbb\xbb")
    payload = {"images": [base64.b64encode(a).decode(),
                          "!!not-base64!!",                  # skipped
                          base64.b64encode(b).decode()],
               "note": "yogurt with lunch"}
    with ingest.app.test_request_context("/ingest", method="POST", json=payload):
        imgs = ingest._extract_images()
        assert [x for x, _ in imgs] == [a, b]        # both photos, junk skipped
        assert imgs[0][1] == "image/jpeg"            # sniffed mime
        assert ingest._extract_note() == "yogurt with lunch"


def test_json_with_no_images_is_still_text_only():
    with ingest.app.test_request_context(
            "/ingest", method="POST", json={"note": "just oatmeal"}):
        assert ingest._extract_images() == []        # text-only path preserved


def test_sniff_mime_by_magic_bytes():
    assert ingest._sniff_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert ingest._sniff_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"


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
    # one photo, no note => router + base rubric (+ the always-on save-template
    # rule) + the body section, which every image prompt carries
    plain = ingest._build_prompt(1, "")
    assert plain == (ingest.ROUTER_PREFIX + ingest.PROMPT
                     + ingest.TEMPLATE_SAVE_SUFFIX + ingest.BODY_SECTION)
    assert "MULTIPLE IMAGES" not in plain and "NOTE:" not in plain
    # several photos => the multi-image block, carrying the count
    multi = ingest._build_prompt(3, "")
    assert "MULTIPLE IMAGES" in multi and "these 3 images" in multi
    assert "NUTRITION LABEL" in multi  # labels are authoritative
    # a note always appends its authoritative block
    assert "only ate half" in ingest._build_prompt(2, "only ate half")
    assert "NOTE: no oil" in ingest._build_prompt(1, "no oil")


def test_build_prompt_adds_meal_time_block_for_a_photo_with_note_and_now():
    from datetime import datetime
    now = datetime(2026, 7, 13, 15, 0)
    # photo + note + now => the model is asked to infer the hour, capped at now
    p = ingest._build_prompt(1, "this yogurt with my lunch", now)
    assert "MEAL TIME" in p and "15:00" in p and "lunch ~13:00" in p
    # no note => no meal-time block even if now is given (plain photo keeps capture time)
    assert "MEAL TIME" not in ingest._build_prompt(1, "", now)
    # note but no now (e.g. tests) => note block only, no meal-time block
    assert "MEAL TIME" not in ingest._build_prompt(1, "only ate half")


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
_GOOD_MEAL = ('{"reasoning":"x","items":[{"name":"rice","portion_g":100,'
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
    fm = _fake_genai(["{ truncated json", _GOOD_MEAL], monkeypatch)
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
    fm = _fake_genai([Exception("503 UNAVAILABLE high demand"), _GOOD_MEAL],
                     monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    monkeypatch.setenv("GEMINI_RETRIES", "3")
    nut = ingest._run_models(["prompt"])
    assert nut["foods"] == "rice"
    assert fm.calls == ["m1", "m1"]  # retried, then succeeded


def test_deadline_returns_before_request_timeout(monkeypatch):
    fm = _fake_genai([_GOOD_MEAL], monkeypatch)
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


def test_exact_duplicate_matches_same_photo_and_note_only():
    sha = "abc123def456"
    rows = [{"image_sha": sha, "foods": "ice cream", "note": "15 spoons"}]
    # same photo AND same note => genuine double-send
    assert ingest._exact_duplicate(sha, "15 spoons", rows)
    # SAME photo, CHANGED note => a correction, NOT a duplicate (must re-analyse)
    assert not ingest._exact_duplicate(sha, "actually 20 spoons", rows)
    # a failed stub with the same hash must never block a retry
    for stub in ("analysis failed", "not food"):
        assert not ingest._exact_duplicate(
            sha, "x", [{"image_sha": sha, "foods": stub, "note": "x"}])
    # different photo never matches
    assert not ingest._exact_duplicate("other", "15 spoons", rows)


def test_meal_row_index_finds_the_prior_row_for_upsert():
    values = [
        ["datetime", "foods", "image_sha", "note"],
        ["t1", "apple", "shaA", "n1"],
        ["t2", "analysis failed", "shaB", "n2"],   # stub — skipped
        ["t3", "yogurt", "shaB", "n3"],            # real meal with shaB
    ]
    assert ingest._meal_row_index(values, "shaA") == 2      # 1-based sheet row
    assert ingest._meal_row_index(values, "shaB") == 4      # skips the stub row
    assert ingest._meal_row_index(values, "missing") is None


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


# -- body composition: the scale-app screenshot --------------------------------
from datetime import datetime, timezone, timedelta  # noqa: E402

_LISBON = timezone(timedelta(hours=1))

# Exactly what the Goodvibes screen showed on 2026-07-04 at 19:03.
_SCREEN = {
    "measured_at": "2026-07-04T19:03",
    "weight_kg": 70.05, "bmi": 22.1, "body_fat_pct": 19.5,
    "subcutaneous_fat_pct": 17.6, "visceral_fat": 5, "body_water_pct": 58.1,
    "muscle_mass_kg": 53.6, "bone_mass_kg": 2.82, "bmr_kcal": 1588,
    "metabolic_age": 19,
}


def test_body_metrics_mirror_the_sheet_schema():
    # ingest is a separate image and can't import src, so the metric list is
    # duplicated. If the two drift, values land in the wrong columns.
    from src.sheets import BODY_METRICS, DAILY_HEADERS
    assert list(ingest.BODY_METRICS) == BODY_METRICS
    for metric in ingest.BODY_METRICS:
        assert metric in DAILY_HEADERS
    # the two columns ingest derives/stamps rather than reads must exist too
    assert "lean_mass_kg" in DAILY_HEADERS and "body_measured_at" in DAILY_HEADERS


def test_normalize_body_reads_every_metric_off_the_screen():
    body = ingest._normalize_body(_SCREEN)
    assert len(body) == 10                     # all ten, none lost
    assert body["weight_kg"] == 70.05          # exact digits, not rounded to 70.1
    assert body["bone_mass_kg"] == 2.82
    assert body["visceral_fat"] == 5
    assert body["bmr_kcal"] == 1588
    assert "measured_at" not in body           # the timestamp is not a metric


def test_normalize_body_drops_implausible_readings():
    # the failure this guard exists for: a dropped decimal, and the "+ 5.35 kg"
    # delta printed above the real weight being read as the weight itself.
    body = ingest._normalize_body({
        "weight_kg": 7005,        # decimal lost -> not a body
        "body_fat_pct": 5.0,      # actually the "+5.0%" delta, but plausible: kept
        "bmi": 0,                 # below the band -> dropped
        "bone_mass_kg": 2.82,     # fine
        "metabolic_age": 19,
    })
    assert "weight_kg" not in body
    assert "bmi" not in body
    assert body["bone_mass_kg"] == 2.82 and body["metabolic_age"] == 19


def test_normalize_body_ignores_junk_and_unknown_keys():
    body = ingest._normalize_body({
        "weight_kg": "70.05",       # string, not a number -> dropped
        "muscle_mass_kg": True,     # bool is an int subclass -> must not become 1
        "protein_pct": 17.0,        # a metric we don't track -> dropped
        "body_fat_pct": 19.5,
    })
    assert body == {"body_fat_pct": 19.5}
    assert ingest._normalize_body(None) == {}
    assert ingest._normalize_body("nope") == {}


def test_resolve_measured_at_uses_the_screens_own_timestamp():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=_LISBON)
    # the reading is dated on screen -> that wins over "when the photo was sent",
    # so weighing at 07:00 and sending at noon still lands on the right day/hour
    assert ingest._resolve_measured_at("2026-07-04T19:03", now) == \
        datetime(2026, 7, 4, 19, 3, tzinfo=_LISBON)
    # no date on screen, or unreadable -> fall back to now
    assert ingest._resolve_measured_at("", now) == now
    assert ingest._resolve_measured_at("ontem à noite", now) == now
    # a future timestamp is never trusted (it would create tomorrow's row)
    assert ingest._resolve_measured_at("2026-12-25T10:00", now) == now


def test_body_row_keys_the_day_and_derives_lean_mass():
    measured = datetime(2026, 7, 4, 19, 3, tzinfo=_LISBON)
    row = ingest._body_row(ingest._normalize_body(_SCREEN), measured)
    assert row["date"] == "2026-07-04"                  # the reading's own day
    assert row["body_measured_at"] == "2026-07-04T19:03+01:00"
    assert row["weight_kg"] == 70.05
    # lean mass isn't on the screen — it's derived: 70.05 * (1 - 0.195)
    assert row["lean_mass_kg"] == 56.39


def test_body_row_skips_lean_mass_without_both_inputs():
    measured = datetime(2026, 7, 4, 19, 3, tzinfo=_LISBON)
    row = ingest._body_row({"weight_kg": 70.0}, measured)   # no body fat read
    assert "lean_mass_kg" not in row


def test_run_models_routes_a_scale_screenshot_to_the_body_path(monkeypatch):
    import json
    reply = json.dumps({"kind": "body", "reasoning": "read the scale screen",
                        "body": _SCREEN, "items": [], "confidence": 0})
    _fake_genai([reply], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    rec = ingest._run_models(["prompt"])
    assert rec["kind"] == "body"
    assert rec["measured_at"] == "2026-07-04T19:03"
    assert rec["body"]["weight_kg"] == 70.05
    assert "foods" not in rec            # never assembled as a meal


def test_text_only_path_cannot_be_hijacked_into_a_body_reading(monkeypatch):
    # no image => a "body" verdict could only be a hallucination; force meal.
    import json
    reply = json.dumps({"kind": "body", "reasoning": "x", "body": _SCREEN,
                        "items": [], "confidence": 0})
    _fake_genai([reply], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    rec = ingest._run_models(["prompt"], allow_body=False)
    assert rec["kind"] == "meal"
    assert rec["foods"] == "not food"    # no items -> logged as nothing


def test_unclassified_reply_defaults_to_meal(monkeypatch):
    _fake_genai([_GOOD_MEAL], monkeypatch)   # carries no `kind` at all
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    assert ingest._run_models(["prompt"])["kind"] == "meal"


def test_body_section_defuses_the_delta_block_and_forbids_guessing():
    section = ingest.BODY_SECTION
    # the trap: "+ 5.35 kg Peso" sits above the real weight, labelled the same
    assert "SINCE" in section.upper() and "DIFFERENCES" in section
    assert "NEVER infer" in section and "OMIT it" in section
    # every metric we store must be named for the model, or it can't fill it
    for metric in ingest.BODY_METRICS:
        assert metric in section
    # the router forks before either rubric is read
    assert '`kind` to "body"' in ingest.ROUTER_PREFIX
    assert '`kind` to "meal"' in ingest.ROUTER_PREFIX


def test_col_letter_reaches_past_z():
    # daily_summary is 40 columns wide — the body block lives past Z
    assert ingest._col_letter(0) == "A"
    assert ingest._col_letter(25) == "Z"
    assert ingest._col_letter(26) == "AA"
    assert ingest._col_letter(39) == "AN"


# -- bowel-movement note (the plain-text "fiz cocó" log) -----------------------
def test_a_text_note_can_be_classified_as_a_bowel_movement(monkeypatch):
    import json
    reply = json.dumps({"kind": "bowel", "reasoning": "user reported a poop",
                        "items": [], "confidence": 0})
    _fake_genai([reply], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    rec = ingest._run_models(["prompt"], allow_body=False, allow_bowel=True)
    assert rec["kind"] == "bowel"
    assert "foods" not in rec           # never assembled as a meal


def test_bowel_verdict_is_ignored_on_the_image_path(monkeypatch):
    # images never enable the bowel fork; a stray "bowel" verdict falls back to meal
    import json
    reply = json.dumps({"kind": "bowel", "reasoning": "x", "items": [],
                        "confidence": 0})
    _fake_genai([reply], monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    rec = ingest._run_models(["prompt"])   # allow_bowel defaults False
    assert rec["kind"] == "meal"
    assert rec["foods"] == "not food"


def test_a_food_note_still_logs_as_a_meal_not_a_bowel_movement(monkeypatch):
    # the fork must not swallow real meals — a food description stays a meal
    _fake_genai([_GOOD_MEAL], monkeypatch)   # kind absent -> meal
    monkeypatch.setenv("GEMINI_MODELS", "m1")
    rec = ingest._run_models(["prompt"], allow_body=False, allow_bowel=True)
    assert rec["kind"] == "meal"
    assert rec["foods"] == "rice"


def test_analyze_text_prepends_the_router_and_opens_the_bowel_fork(monkeypatch):
    from datetime import datetime
    captured = {}

    def fake_run(contents, **kw):
        captured["prompt"] = contents[0]
        captured["kw"] = kw
        return {"kind": "meal"}

    monkeypatch.setattr(ingest, "_run_models", fake_run)
    ingest.analyze_text("fiz cocó", datetime(2026, 7, 15, 9, 0))
    assert captured["prompt"].startswith(ingest.TEXT_ROUTER_PREFIX)
    assert captured["kw"]["allow_bowel"] is True      # text can be a bowel log…
    assert captured["kw"]["allow_body"] is False      # …but never a scale reading


def test_text_router_offers_the_bowel_classification():
    r = ingest.TEXT_ROUTER_PREFIX
    assert '`kind` to "bowel"' in r
    assert '`kind` to "meal"' in r
    assert "cocó" in r                 # multilingual — the user writes Portuguese
    # a food note is explicitly kept as a meal even if it mentions the bathroom
    assert "in passing" in r


# -- the read API (what the iOS app talks to) ----------------------------------
_DAILY_GRID = [
    ["date", "bowel_movement", "sleep_mins", "hrv_ms",
     "steps", "weight_kg", "total_cals_in", "updated_at"],
    ["2026-07-15", "TRUE", 470, 75.2, 4151, 70.0, 2000, "x"],
    ["2026-07-16", "", 525, 73.1, 866, "", 1800, "x"],
]


def _api(monkeypatch, grid=None):
    monkeypatch.setattr(ingest, "_read_tab", lambda tab: grid or _DAILY_GRID)
    monkeypatch.setenv("INGEST_TOKEN", "t")
    monkeypatch.setenv("HEALTH_SPREADSHEET_ID", "sid")
    return ingest.app.test_client()


_HDR = {"X-Auth-Token": "t"}


def test_daily_requires_the_token():
    assert ingest.app.test_client().get("/daily").status_code == 401
    assert ingest.app.test_client().get("/schema").status_code == 401


def test_daily_returns_days_nested_by_block(monkeypatch):
    r = _api(monkeypatch).get("/daily?from=2026-07-15&to=2026-07-16", headers=_HDR)
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 2
    day = body["days"][0]
    assert day["date"] == "2026-07-15"
    # blocks are the natural shape of the domain — and of the generated Swift
    assert day["sleep"]["sleep_mins"] == 470
    assert day["recovery"]["hrv_ms"] == 75.2
    assert day["activity"]["steps"] == 4151
    assert day["self_report"]["bowel_movement"] is True


def test_blank_cells_become_null_not_zero(monkeypatch):
    # A day with no weigh-in has NO body composition. A 0 there is a lie the app
    # would happily chart as a 69 kg weight loss.
    body = _api(monkeypatch).get("/daily?from=2026-07-16&to=2026-07-16",
                                 headers=_HDR).get_json()
    day = body["days"][0]
    assert day["body"]["weight_kg"] is None
    assert day["self_report"]["bowel_movement"] is None
    assert day["sleep"]["sleep_mins"] == 525          # present values survive


def test_values_are_typed_from_the_schema(monkeypatch):
    day = _api(monkeypatch).get("/daily?from=2026-07-15&to=2026-07-15",
                                headers=_HDR).get_json()["days"][0]
    assert day["self_report"]["bowel_movement"] is True    # "TRUE" -> bool
    assert isinstance(day["sleep"]["sleep_mins"], int)     # integer dtype
    assert isinstance(day["recovery"]["hrv_ms"], float)    # number dtype


def test_the_app_can_ask_for_only_the_blocks_it_draws(monkeypatch):
    body = _api(monkeypatch).get("/daily?blocks=sleep,recovery", headers=_HDR).get_json()
    assert body["blocks"] == ["sleep", "recovery"]
    day = body["days"][0]
    assert "sleep" in day and "recovery" in day
    assert "nutrition" not in day and "activity" not in day


def test_unknown_block_is_rejected_with_the_valid_list(monkeypatch):
    r = _api(monkeypatch).get("/daily?blocks=sleep,bogus", headers=_HDR)
    assert r.status_code == 400
    assert "bogus" in str(r.get_json()["error"])
    assert "sleep" in r.get_json()["known"]


def test_tier1_trims_to_the_headline_metrics(monkeypatch):
    day = _api(monkeypatch).get("/daily?tier=1&blocks=recovery",
                                headers=_HDR).get_json()["days"][0]
    assert "hrv_ms" in day["recovery"]                 # tier 1
    assert "hrv_entropy" not in day["recovery"]        # tier 2


def test_bad_dates_are_rejected(monkeypatch):
    r = _api(monkeypatch).get("/daily?from=15-07-2026", headers=_HDR)
    assert r.status_code == 400


def test_date_range_filters(monkeypatch):
    body = _api(monkeypatch).get("/daily?from=2026-07-16&to=2026-07-16",
                                 headers=_HDR).get_json()
    assert body["count"] == 1 and body["days"][0]["date"] == "2026-07-16"


def test_schema_endpoint_teaches_a_client_the_causal_rule(monkeypatch):
    body = _api(monkeypatch).get("/schema", headers=_HDR).get_json()
    cols = {c["name"]: c for c in body["columns"]}
    assert cols["sleep_mins"]["causal_role"] == "outcome"
    assert "night that ended" in cols["sleep_mins"]["measures_when"]
    assert cols["total_cals_in"]["causal_role"] == "input"
    assert cols["resting_hr_bpm"]["direction"] == "down_good"
    assert cols["weight_kg"]["unit"] == "kg"
    assert {b["name"] for b in body["blocks"]} >= {"sleep", "recovery", "body"}
