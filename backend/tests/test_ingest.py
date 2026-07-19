"""Unit tests for the ingest service's pure helpers.

ingest/main.py initialises all clients lazily, so importing it needs no env
vars or credentials — that property is itself asserted here.
"""
import importlib.util
import json
import pathlib

import pytest
from google.genai import errors as genai_errors

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


def _api_error(code, status, details=None):
    """A real google-genai APIError, as the SDK would raise it."""
    return genai_errors.APIError(
        code, {"error": {"code": code, "status": status,
                         "message": status, **(details or {})}})


def test_retry_same_model_only_for_google_side_capacity_errors():
    # Their capacity problem: wait it out on the same model.
    assert ingest._retry_same_model(_api_error(503, "UNAVAILABLE"))
    assert ingest._retry_same_model(_api_error(500, "INTERNAL"))
    assert ingest._retry_same_model(_api_error(504, "DEADLINE_EXCEEDED"))
    # Our quota. Another call cannot succeed and only digs deeper — end the
    # model's turn and let the queue's backoff (or another model's bucket) fix it.
    assert not ingest._retry_same_model(_api_error(429, "RESOURCE_EXHAUSTED"))
    # Never fixes itself.
    assert not ingest._retry_same_model(_api_error(404, "NOT_FOUND"))
    assert not ingest._retry_same_model(_api_error(400, "INVALID_ARGUMENT"))
    # A bare socket/SSL error is transient — no status to read, assume capacity.
    assert ingest._retry_same_model(ConnectionResetError("reset by peer"))


def test_a_quota_errors_details_cannot_be_misread_as_a_permanent_400():
    # Why we classify on .code, not on str(err): the SDK stringifies the whole
    # details JSON, and a real quota error's details carry numbers like 400/404 as
    # VALUES. The old substring sniff read that as a permanent 400 and skipped the
    # model; it must be seen as the 429 it is.
    err = _api_error(429, "RESOURCE_EXHAUSTED", {
        "details": [{"quotaValue": "400", "quotaId": "GenerateRequestsPerDay"}]})
    assert "400" in str(err)                       # the trap
    assert err.code == 429
    assert not ingest._retry_same_model(err)       # treated as quota, not as 400
    # ...and a 503 whose details merely mention 400 must still be retried.
    over = _api_error(503, "UNAVAILABLE", {"details": [{"retryHint": "400ms"}]})
    assert "400" in str(over) and ingest._retry_same_model(over)


def test_a_429_ends_the_models_turn_instead_of_hammering(monkeypatch):
    # The free tier is ~10 req/min per PROJECT. On a held-out attempt (one model)
    # this ends the attempt -> 5xx -> Cloud Tasks waits 5-120s, which is the actual
    # cure for a rolling-window 429.
    fm = _fake_genai([_api_error(429, "RESOURCE_EXHAUSTED")] * 8, monkeypatch)
    monkeypatch.setenv("GEMINI_MODELS", "best")
    with pytest.raises(RuntimeError):
        ingest._run_models(["prompt"], models=["best"], retries=6)
    assert fm.calls == ["best"]  # exactly one call, not six

    # On a chain-walking attempt it moves to the next model, whose quota is a
    # separate bucket — the one thing that CAN work while we're rate-limited.
    fm = _fake_genai([_api_error(429, "RESOURCE_EXHAUSTED"), _GOOD_MEAL], monkeypatch)
    nut = ingest._run_models(["prompt"], models=["best", "steady"], retries=6)
    assert fm.calls == ["best", "steady"] and nut["model"] == "steady"


class _FakeRunApi:
    """Stands in for the Cloud Run Admin API."""

    def __init__(self, executions=(), ok=True):
        self.executions = list(executions)
        self.ok = ok
        self.runs = []
        self.gets = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        return type("R", (), {
            "ok": True, "status_code": 200,
            "json": lambda _s=None: {"executions": self.executions},
        })()

    def post(self, url, json=None, timeout=None):
        self.runs.append(url)
        return type("R", (), {"ok": self.ok, "status_code": 200 if self.ok else 500,
                              "text": "boom"})()


def _wire_run_api(monkeypatch, api):
    monkeypatch.setenv("GCP_PROJECT", "proj")
    monkeypatch.setenv("DAILY_JOB", "health-tracker-daily")
    monkeypatch.setenv("DAILY_JOB_LOCATION", "europe-west1")
    monkeypatch.setattr(ingest.google.auth, "default", lambda scopes=None: (None, "proj"))
    import google.auth.transport.requests as gart
    monkeypatch.setattr(gart, "AuthorizedSession", lambda creds: api)


def test_a_weigh_in_wakes_the_daily_sync(monkeypatch):
    # The weigh-in IS the wake signal — the whole reason sleep now lands on its own
    # day instead of ~24h late.
    api = _FakeRunApi()
    _wire_run_api(monkeypatch, api)
    ingest._trigger_daily_sync("2026-07-17")
    assert api.runs == ["https://run.googleapis.com/v2/projects/proj/locations/"
                        "europe-west1/jobs/health-tracker-daily:run"]


def test_a_second_weigh_in_does_not_start_a_concurrent_sync(monkeypatch):
    # upsert_daily is read-modify-write against a grid snapshot: two overlapping
    # runs would both miss a new date and append it TWICE.
    api = _FakeRunApi(executions=[{"name": "x/executions/run-1"}])  # no completionTime
    _wire_run_api(monkeypatch, api)
    ingest._trigger_daily_sync("2026-07-17")
    assert api.runs == []  # refused to pile on


def test_a_finished_previous_sync_does_not_block_a_new_one(monkeypatch):
    api = _FakeRunApi(executions=[{"name": "x/executions/run-1",
                                   "completionTime": "2026-07-17T06:01:00Z"}])
    _wire_run_api(monkeypatch, api)
    ingest._trigger_daily_sync("2026-07-17")
    assert len(api.runs) == 1


def test_a_failed_trigger_never_breaks_the_weigh_in(monkeypatch):
    # The weight is already written and this runs on the Cloud Tasks worker —
    # raising would retry the task and rewrite the row. The backstop covers it.
    _wire_run_api(monkeypatch, _FakeRunApi(ok=False))
    ingest._trigger_daily_sync("2026-07-17")  # must not raise

    monkeypatch.setattr(ingest.google.auth, "default",
                        lambda scopes=None: (_ for _ in ()).throw(RuntimeError("no adc")))
    ingest._trigger_daily_sync("2026-07-17")  # must not raise


def test_backoff_is_exponential_capped_and_jittered(monkeypatch):
    monkeypatch.delenv("GEMINI_BACKOFF_BASE", raising=False)
    monkeypatch.delenv("GEMINI_BACKOFF_CAP_S", raising=False)
    monkeypatch.delenv("GEMINI_BACKOFF_JITTER_S", raising=False)
    base, cap = ingest.DEFAULT_BACKOFF_BASE, ingest.DEFAULT_BACKOFF_CAP_S
    jit = ingest.DEFAULT_BACKOFF_JITTER_S

    for n in range(1, 6):
        waits = {ingest._backoff_s(n) for _ in range(40)}
        floor = min(base ** n, cap)
        # jitter only ever ADDS delay, so the measured request rate can't rise
        assert all(floor <= w <= floor + jit for w in waits)
        assert len(waits) > 1, "no jitter — every client retries in lockstep"
    # capped, so one attempt can't sleep away its whole deadline
    assert ingest._backoff_s(99) <= cap + jit


def test_default_chain_is_best_first_and_free_tier():
    # 3.5-flash is the model we want the numbers from; flash-lite is the steady
    # fallback (2026-07-12/13 incidents). Nothing waits on this now, so accuracy
    # leads. No Pro (429s on the free key).
    chain = ingest.DEFAULT_MODELS.split(",")
    assert chain[0] == "gemini-3.5-flash"
    assert chain[1] == "gemini-3.1-flash-lite"
    assert not any("pro" in m for m in chain)


def test_worker_holds_out_for_the_best_model_until_patience_runs_out(monkeypatch):
    monkeypatch.setenv("GEMINI_MODELS", "best,steady,last-resort")
    monkeypatch.setenv("TASKS_MAX_ATTEMPTS", "8")
    monkeypatch.setenv("GEMINI_FALLBACK_LAST_N", "2")

    # attempts 1-6 (0-based 0..5): the best model ONLY. Falling through to a
    # weaker model here would answer with it and waste the whole retry window.
    for attempt in range(6):
        kw = ingest._worker_kwargs(attempt)
        assert kw["models"] == ["best"], f"attempt {attempt} settled early"
        assert kw["retries"] > 1  # spend the attempt on it

    # attempts 7-8: out of patience — walk the chain, one shot each, so a row
    # lands instead of the stub attempt 8 would write.
    for attempt in (6, 7):
        kw = ingest._worker_kwargs(attempt)
        assert kw["models"] == ["best", "steady", "last-resort"]
        assert kw["retries"] == 1


def test_worker_walks_the_chain_when_there_is_only_one_attempt(monkeypatch):
    # A queue with no retries has no patience to spend: never hold out, because
    # this attempt is also the last one.
    monkeypatch.setenv("GEMINI_MODELS", "best,steady")
    monkeypatch.setenv("TASKS_MAX_ATTEMPTS", "1")
    assert ingest._worker_kwargs(0)["models"] == ["best", "steady"]


def test_fallback_attempt_reaches_a_weaker_model_even_when_the_best_one_hangs(
        monkeypatch):
    # The reason the last attempts use retries=1: with the models timing out at
    # GEMINI_TIMEOUT_MS each, a second retry of the best model would burn the
    # deadline and we'd never reach flash-lite — turning a row into a stub. Every
    # call here hangs the full 60 s against a 105 s deadline.
    clock = {"t": 0.0}
    monkeypatch.setattr(ingest.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ingest.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))

    class _Hangs:
        def generate_content(self, model, contents, config):
            calls.append(model)
            clock["t"] += 60.0
            raise Exception("503 UNAVAILABLE")

    calls = []
    monkeypatch.setattr(ingest, "_genai",
                        lambda: type("C", (), {"models": _Hangs()})())
    monkeypatch.setenv("GEMINI_MODELS", "best,steady")
    monkeypatch.setenv("TASKS_MAX_ATTEMPTS", "8")
    monkeypatch.setenv("GEMINI_DEADLINE_S", "105")

    with pytest.raises(RuntimeError):
        ingest._run_models(["prompt"], **ingest._worker_kwargs(7))  # final attempt
    assert calls == ["best", "steady"]  # not ["best", "best", ...]


def test_analysis_budget_counts_from_the_start_of_the_request(monkeypatch):
    # The deadline must cover the sheet reads and Drive downloads that precede
    # analysis, or a slow read pushes the response past Cloud Run's 180 s and the
    # final attempt 504s without ever writing its stub.
    monkeypatch.setenv("GEMINI_DEADLINE_S", "105")
    monkeypatch.setattr(ingest.time, "monotonic", lambda: 1000.0)
    assert ingest._analysis_budget(970.0) == 75.0  # 30 s of I/O already spent
    assert ingest._analysis_budget(800.0) < 0      # overrun => give up at once


def test_run_models_respects_model_override(monkeypatch):
    # a held-out attempt must call ONLY the model it's given, not the whole chain
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


class _FakeSheetsSvc:
    """Records the two request shapes `_heal_daily_duplicates` issues, keyed by
    whether `.values()` was chained before `.batchUpdate()` (a values write) or
    not (a structural request like deleteDimension)."""

    def __init__(self):
        self.value_batch_bodies = []
        self.sheet_batch_bodies = []
        self._in_values = False

    def spreadsheets(self):
        self._in_values = False
        return self

    def values(self):
        self._in_values = True
        return self

    def batchUpdate(self, spreadsheetId, body):
        (self.value_batch_bodies if self._in_values else self.sheet_batch_bodies).append(body)
        return self

    def execute(self):
        return {}


def test_heal_daily_duplicates_merges_and_deletes_the_extra_row(monkeypatch):
    # Reproduces the reported bug: a weigh-in's own row (weight_kg only) and the
    # daily job's row (sleep_mins only) both landed under the same date.
    svc = _FakeSheetsSvc()
    monkeypatch.setattr(ingest, "_sheets", lambda: svc)
    monkeypatch.setattr(ingest, "_sid", lambda: "sid")
    monkeypatch.setattr(ingest, "_tab_id", lambda tab: 7)

    header = ["date", "sleep_mins", "weight_kg"]
    row_a = ["2026-07-19", "", 70.5]
    row_b = ["2026-07-19", 420, ""]
    grid = [header, row_a, row_b]

    healed = ingest._heal_daily_duplicates(grid)

    assert healed == [header, ["2026-07-19", 420, 70.5]]
    assert svc.value_batch_bodies == [{
        "valueInputOption": "RAW",
        "data": [{"range": "daily_summary!A2:C2",
                  "values": [["2026-07-19", 420, 70.5]]}],
    }]
    assert svc.sheet_batch_bodies == [{"requests": [{"deleteDimension": {"range": {
        "sheetId": 7, "dimension": "ROWS", "startIndex": 2, "endIndex": 3,
    }}}]}]


def test_heal_daily_duplicates_is_a_noop_without_duplicates(monkeypatch):
    def _no_api_calls():
        raise AssertionError("must not touch the Sheets API without a duplicate")
    monkeypatch.setattr(ingest, "_sheets", lambda: _no_api_calls())

    header = ["date", "weight_kg"]
    grid = [header, ["2026-07-19", 70.5], ["2026-07-20", 71.0]]
    assert ingest._heal_daily_duplicates(grid) is grid


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


# The meals tab the /meals endpoint reads: two meals today (out of order, to
# prove sorting), one stub, and one from yesterday (to prove date filtering).
_MEALS_GRID = [
    ingest.MEALS_HEADERS,
    ["2026-07-18T13:00:00+01:00", "Chicken", "[]", 500, 40, 10, 20,
     0.9, "m", "", 300, "sha2", "big lunch", ""],
    ["2026-07-18T08:30:00+01:00", "Oats", "[]", 300, 10, 50, 5,
     0.8, "m", "", 200, "sha1", "", ""],
    ["2026-07-18T20:00:00+01:00", "analysis failed", "[]", 0, 0, 0, 0,
     0, "m", "", 0, "sha3", "", ""],
    ["2026-07-17T12:00:00+01:00", "Yesterday", "[]", 100, 5, 5, 5,
     0.5, "m", "", 100, "sha4", "", ""],
]


def test_meals_requires_the_token():
    assert ingest.app.test_client().get("/meals").status_code == 401


def test_meals_rejects_a_bad_date(monkeypatch):
    r = _api(monkeypatch, _MEALS_GRID).get("/meals?date=18-07-2026", headers=_HDR)
    assert r.status_code == 400


def test_meals_lists_the_day_sorted_without_stubs_or_other_days(monkeypatch):
    body = _api(monkeypatch, _MEALS_GRID).get(
        "/meals?date=2026-07-18", headers=_HDR).get_json()
    assert body["date"] == "2026-07-18"
    assert body["count"] == 2                       # stub + yesterday dropped
    assert [m["foods"] for m in body["meals"]] == ["Oats", "Chicken"]  # sorted
    assert body["meals"][0]["time"] == "08:30"      # HH:MM off the ISO string
    assert body["meals"][1]["note"] == "big lunch"


def test_meals_totals_match_the_listed_meals(monkeypatch):
    body = _api(monkeypatch, _MEALS_GRID).get(
        "/meals?date=2026-07-18", headers=_HDR).get_json()
    assert body["totals"] == {"calories": 800.0, "protein_g": 50.0,
                              "carbs_g": 60.0, "fat_g": 25.0}


# -- targets: the per-metric goals (the foundation of the whole app) ------------
def test_targets_tab_headers_match_the_spec():
    assert ingest.TARGETS_TAB_HEADERS == [
        "metric", "kind", "floor", "ceiling", "unit", "source"]
    assert ingest.TARGETS_LAST_COL == "F"


def test_micro_targets_are_adult_male_references():
    micro = ingest._micro_target_dict()
    # a reach floor is the RDA/AI; a limit carries a ceiling instead
    assert micro["iron_mg"] == {"kind": "reach", "unit": "mg",
                                "source": "rda", "floor": 8.0}     # male RDA
    assert micro["sodium_mg"]["kind"] == "limit"
    assert micro["sodium_mg"]["ceiling"] == 2300.0 and "floor" not in micro["sodium_mg"]
    assert micro["vitamin_c_mg"]["floor"] == 90.0
    assert micro["vitamin_d_ug"]["floor"] == 15.0
    assert micro["potassium_mg"]["floor"] == 3400.0
    # added sugar / saturated fat scale with energy -> DERIVED, not in this table
    assert "added_sugar_g" not in micro and "saturated_fat_g" not in micro
    assert len(micro) == len(ingest._MICRO_TARGETS)


def test_derive_targets_from_measured_data():
    # a rolling TDEE from measured total_cals_out; body from the latest weigh-in
    daily = [
        {"total_cals_out": 2400, "weight_kg": 70.5, "lean_mass_kg": 56.5, "bmr_kcal": 1588},
        {"total_cals_out": 2600, "weight_kg": 70.2, "lean_mass_kg": 56.4},
        {"total_cals_out": 2200, "weight_kg": 70.0, "lean_mass_kg": 56.3},
    ]
    t, b = ingest._derive_targets(daily)
    assert b["tdee_kcal"] == 2400.0                 # mean of the three days
    assert b["calorie_target_kcal"] == 2100.0       # 12.5% below TDEE
    assert b["weight_kg"] == 70.0                    # LATEST, not the mean
    assert b["lean_mass_kg"] == 56.3
    assert b["goal"] == "recomp" and b["protein_g_per_kg"] == 2.0
    assert t["protein_g"] == {"kind": "reach", "floor": 140.0, "unit": "g",
                              "source": "measured"}          # 2.0 g/kg * 70
    assert t["fat_g"]["floor"] == 56.0                       # 0.8 * 70
    assert t["calories"]["kind"] == "window"
    assert t["calories"]["floor"] == 1920.0 and t["calories"]["ceiling"] == 2280.0
    assert t["fiber_g"]["floor"] == 29.0                     # 14 g / 1000 kcal
    assert t["saturated_fat_g"]["kind"] == "limit"           # ceiling, not a floor
    assert "ceiling" in t["saturated_fat_g"] and "floor" not in t["saturated_fat_g"]


def test_derive_targets_falls_back_without_history():
    t, b = ingest._derive_targets([])
    assert b["tdee_kcal"] == ingest.DEFAULT_TDEE
    assert b["weight_kg"] == ingest.DEFAULT_WEIGHT_KG
    assert b["lean_mass_kg"] is None
    assert t["protein_g"]["floor"] == 140.0     # 2.0 * default 70
    # a scale BMR alone still beats the flat default TDEE
    _, b2 = ingest._derive_targets([{"bmr_kcal": 1600}])
    assert b2["tdee_kcal"] == float(round(1600 * ingest.BMR_TO_TDEE))


def test_todays_consumed_sums_macros_and_micros_excluding_stubs():
    rows = [
        {"foods": "oats", "calories": 300, "protein_g": 10, "carbs_g": 50, "fat_g": 5,
         "items": json.dumps([{"name": "oats", "portion_g": 80, "calories": 300,
                               "protein_g": 10, "carbs_g": 50, "fat_g": 5,
                               "nutrients": {"fiber_g": 8, "iron_mg": 2.0}}])},
        {"foods": "chicken", "calories": 250, "protein_g": 45, "carbs_g": 0, "fat_g": 6,
         "items": json.dumps([{"name": "chicken", "portion_g": 150, "calories": 250,
                               "protein_g": 45, "carbs_g": 0, "fat_g": 6,
                               "nutrients": {"iron_mg": 1.0, "zinc_mg": 3}}])},
        {"foods": "analysis failed", "calories": 0, "protein_g": 0, "carbs_g": 0,
         "fat_g": 0, "items": "[]"},                          # stub -> skipped
        {"foods": "ghost", "calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    ]
    c = ingest._todays_consumed(rows)
    assert c["calories"] == 550.0 and c["protein_g"] == 55.0
    assert c["carbs_g"] == 50.0 and c["fat_g"] == 11.0
    assert c["fiber_g"] == 8.0
    assert c["iron_mg"] == 3.0          # 2.0 + 1.0, summed across meals' items
    assert c["zinc_mg"] == 3.0
    assert "calcium_mg" not in c        # nothing supplied it -> omitted, not a zero


def test_targets_from_grid_parses_and_omits_blanks():
    grid = [ingest.TARGETS_TAB_HEADERS,
            ["protein_g", "reach", 140, "", "g", "measured"],
            ["sodium_mg", "limit", "", 2300, "mg", "rda"],
            ["", "", "", "", "", ""]]                          # blank metric -> ignored
    parsed = ingest._targets_from_grid(grid)
    assert parsed["protein_g"] == {"kind": "reach", "unit": "g",
                                   "source": "measured", "floor": 140.0}
    assert "ceiling" not in parsed["protein_g"]                # blank cell omitted
    assert parsed["sodium_mg"]["ceiling"] == 2300.0
    assert "floor" not in parsed["sodium_mg"]
    assert len(parsed) == 2


def test_resolve_targets_layers_defaults_then_tab_then_measured():
    derived, _ = ingest._derive_targets([{"total_cals_out": 2400, "weight_kg": 70}])
    tab = {
        "calories": {"kind": "window", "floor": 1500.0, "ceiling": 1800.0,
                     "unit": "kcal", "source": "manual"},   # user pinned it
        "iron_mg": {"kind": "reach", "floor": 10.0, "unit": "mg", "source": "rda"},
    }
    final = ingest._resolve_targets(derived, tab)
    assert final["vitamin_c_mg"]["floor"] == 90.0     # default present w/o a tab row
    assert final["iron_mg"]["floor"] == 10.0          # a tab edit of a default wins
    assert final["protein_g"]["floor"] == 140.0       # measured, computed live...
    assert final["calories"]["source"] == "manual"    # ...unless pinned manual
    assert final["calories"]["floor"] == 1500.0


def test_target_seed_rows_only_adds_missing_metrics():
    derived, _ = ingest._derive_targets([])
    rows_all = ingest._target_seed_rows(set(), derived)       # nothing seeded yet
    metrics = {r[0] for r in rows_all}
    assert "iron_mg" in metrics and "protein_g" in metrics
    assert len(rows_all) == len(ingest._MICRO_TARGETS) + len(derived)
    # a metric the user already has is never re-seeded (their edits are law)
    rows_some = ingest._target_seed_rows({"iron_mg", "protein_g"}, derived)
    assert {"iron_mg", "protein_g"}.isdisjoint({r[0] for r in rows_some})
    assert len(rows_some) == len(rows_all) - 2


# -- GET /today (the live daily-screen payload) --------------------------------
_TODAY_DAILY_GRID = [
    ["date", "total_cals_out", "weight_kg", "lean_mass_kg", "bmr_kcal", "updated_at"],
    ["2026-07-16", 2400, 70.5, 56.5, 1588, "x"],
    ["2026-07-17", 2400, 70.0, 56.3, 1588, "x"],
]

_TODAY_MEALS_GRID = [
    ingest.MEALS_HEADERS,
    ["2026-07-18T08:30:00+01:00", "Oats", json.dumps([
        {"name": "oats", "portion_g": 80, "calories": 300, "protein_g": 10,
         "carbs_g": 50, "fat_g": 5, "nutrients": {"fiber_g": 8, "iron_mg": 2.0,
                                                   "sodium_mg": 100}}]),
     300, 10, 50, 5, 0.8, "m", "", 80, "sha1", "", ""],
    ["2026-07-18T13:00:00+01:00", "Chicken & rice", json.dumps([
        {"name": "chicken", "portion_g": 150, "calories": 250, "protein_g": 45,
         "carbs_g": 0, "fat_g": 6, "nutrients": {"iron_mg": 1.0, "sodium_mg": 300,
                                                 "zinc_mg": 3}},
        {"name": "rice", "portion_g": 150, "calories": 200, "protein_g": 4,
         "carbs_g": 44, "fat_g": 1, "nutrients": {"fiber_g": 1.5, "magnesium_mg": 20}}]),
     450, 49, 44, 7, 0.9, "m", "", 300, "sha2", "big lunch", ""],
    ["2026-07-18T20:00:00+01:00", "analysis failed", "[]", 0, 0, 0, 0,
     0, "m", "", 0, "sha3", "", ""],
    ["2026-07-17T12:00:00+01:00", "Yesterday", json.dumps([
        {"name": "x", "portion_g": 100, "calories": 100, "protein_g": 5, "carbs_g": 5,
         "fat_g": 5, "nutrients": {"iron_mg": 99}}]),          # must NOT leak into today
     100, 5, 5, 5, 0.5, "m", "", 100, "sha4", "", ""],
]

# A partly-populated targets tab: one plain rda row, plus two manual overrides.
_TODAY_TARGETS_GRID = [
    ingest.TARGETS_TAB_HEADERS,
    ["iron_mg", "reach", 8, "", "mg", "rda"],
    ["calories", "window", 1500, 1800, "kcal", "manual"],     # user pinned calories
    ["sodium_mg", "limit", "", 2000, "mg", "manual"],         # user tightened sodium
]


def _today_client(monkeypatch, meals=None, daily=None, targets=None):
    grids = {
        ingest.MEALS_TAB: _TODAY_MEALS_GRID if meals is None else meals,
        ingest.DAILY_TAB: _TODAY_DAILY_GRID if daily is None else daily,
        ingest.TARGETS_TAB: _TODAY_TARGETS_GRID if targets is None else targets,
    }
    monkeypatch.setattr(ingest, "_read_tab", lambda tab: grids.get(tab, []))
    monkeypatch.setattr(ingest, "_seed_targets", lambda *a, **k: None)  # no writes
    monkeypatch.setenv("INGEST_TOKEN", "t")
    monkeypatch.setenv("HEALTH_SPREADSHEET_ID", "sid")
    return ingest.app.test_client()


def test_today_requires_the_token():
    assert ingest.app.test_client().get("/today").status_code == 401


def test_today_rejects_a_bad_date(monkeypatch):
    r = _today_client(monkeypatch).get("/today?date=18-07-2026", headers=_HDR)
    assert r.status_code == 400


def test_today_sums_live_consumed_macros_and_micros(monkeypatch):
    body = _today_client(monkeypatch).get(
        "/today?date=2026-07-18", headers=_HDR).get_json()
    assert body["date"] == "2026-07-18"
    assert body["meal_count"] == 2                    # stub + yesterday excluded
    c = body["consumed"]
    assert c["calories"] == 750.0 and c["protein_g"] == 59.0   # 300+450, 10+49
    assert c["carbs_g"] == 94.0 and c["fat_g"] == 12.0
    assert c["fiber_g"] == 9.5                        # 8 + 1.5 across meals
    assert c["iron_mg"] == 3.0                        # 2.0 + 1.0
    assert c["sodium_mg"] == 400.0 and c["zinc_mg"] == 3.0 and c["magnesium_mg"] == 20.0
    assert "vitamin_c_mg" not in c                    # nothing supplied it


def test_today_attaches_targets_layered_over_defaults(monkeypatch):
    t = _today_client(monkeypatch).get(
        "/today?date=2026-07-18", headers=_HDR).get_json()["targets"]
    # a measured macro, computed live from the user's own data
    assert t["protein_g"] == {"kind": "reach", "floor": 140.0, "unit": "g",
                              "source": "measured"}
    # an RDA default present though it isn't in the tab at all
    assert t["vitamin_c_mg"]["floor"] == 90.0 and t["vitamin_c_mg"]["source"] == "rda"
    # a manual override WINS over the measured calorie window
    assert t["calories"]["source"] == "manual"
    assert t["calories"]["floor"] == 1500.0 and t["calories"]["ceiling"] == 1800.0
    # a manual micro override wins over the rda default (2300 -> 2000)
    assert t["sodium_mg"]["ceiling"] == 2000.0 and t["sodium_mg"]["source"] == "manual"


def test_today_basis_exposes_the_derivation_inputs(monkeypatch):
    b = _today_client(monkeypatch).get(
        "/today?date=2026-07-18", headers=_HDR).get_json()["basis"]
    assert b["tdee_kcal"] == 2400.0 and b["weight_kg"] == 70.0
    assert b["protein_g_per_kg"] == 2.0 and b["goal"] == "recomp"


def test_today_meals_carry_per_item_nutrients_for_drilldown(monkeypatch):
    meals = _today_client(monkeypatch).get(
        "/today?date=2026-07-18", headers=_HDR).get_json()["meals"]
    assert [m["foods"] for m in meals] == ["Oats", "Chicken & rice"]  # sorted, no stub
    lunch = meals[1]
    assert {i["name"] for i in lunch["items"]} == {"chicken", "rice"}
    chicken = next(i for i in lunch["items"] if i["name"] == "chicken")
    assert chicken["nutrients"]["zinc_mg"] == 3.0    # the drill-down source


# -- GET /nutrients (the per-nutrient reference knowledge base) -----------------
# Allowed fields on a nutrient row — the app relies on exactly these, so a stray
# key in nutrient_info.json (a typo while populating from the PDF) must fail here
# rather than silently not render.
_INFO_FIELDS = {"summary", "roles", "goal_relevance", "optimal_range", "upper_limit",
                "food_sources", "deficiency", "excess", "tips", "fact", "sections",
                "references"}
# A food source is a structured object (for the future meal-recommendation feature).
_FOOD_SOURCE_FIELDS = {"food", "amount", "unit", "per", "note"}


def test_nutrients_requires_the_token():
    assert ingest.app.test_client().get("/nutrients").status_code == 401


def test_nutrients_returns_info_keyed_by_known_nutrients(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "t")
    body = ingest.app.test_client().get("/nutrients", headers=_HDR).get_json()
    assert "nutrients" in body and isinstance(body["nutrients"], dict)
    # every row's key is a real nutrient (guards a typo in the JSON)
    for key in body["nutrients"]:
        assert key in ingest.NUTRIENT_KEYS, f"unknown nutrient key {key!r}"
    # and every populated field is one the app knows how to render
    for key, entry in body["nutrients"].items():
        assert set(entry).issubset(_INFO_FIELDS), \
            f"{key} has unknown field(s) {set(entry) - _INFO_FIELDS}"
        # food sources are structured objects (food + amount), for future features
        for source in entry.get("food_sources", []):
            assert isinstance(source, dict) and "food" in source and "amount" in source
            assert set(source).issubset(_FOOD_SOURCE_FIELDS), \
                f"{key} food source has unknown field(s) {set(source) - _FOOD_SOURCE_FIELDS}"


def test_nutrients_example_rows_are_populated(monkeypatch):
    # nutrients the reference PDFs cover in depth — the UI must have rich content and
    # the numeric recommendation values (optimal range, upper limit) the user cares about.
    monkeypatch.setenv("INGEST_TOKEN", "t")
    rows = ingest.app.test_client().get("/nutrients", headers=_HDR).get_json()["nutrients"]
    for key in ("iron_mg", "magnesium_mg", "zinc_mg", "vitamin_b12_ug", "omega3_g"):
        entry = rows[key]
        assert entry["summary"]                       # non-empty text
        assert entry["roles"] and isinstance(entry["roles"], list)
        assert entry["optimal_range"] and entry["upper_limit"]
        assert entry["food_sources"] and entry["food_sources"][0]["amount"] > 0


def test_nutrient_info_covers_every_nutrient_as_a_fillable_row():
    # the table is laid out with a row for EVERY nutrient, so populating from the
    # PDF is only ever filling blanks — never adding rows.
    info = ingest._nutrient_info()
    assert set(info["nutrients"]) == set(ingest.NUTRIENT_KEYS)
