"""The app's meal endpoints end to end: /meals/save, /meals/delete, /meals/library
and the queue's estimate of a described ingredient — against an in-memory sheet
that really applies every write, so each test reads back what was stored rather
than what was requested.

What these protect:
  * an edited portion changes the numbers, all of them, not only the label;
  * repeating a meal is instant, and a retried save never logs it twice;
  * a delete or an edit lands on THE meal it names and nowhere else;
  * a changed meal is reflected in its day — live today, re-totalled for a closed
    day — so the app and the sheet never disagree.
"""
import importlib.util
import json
import pathlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "ingest" / "main.py"
_spec = importlib.util.spec_from_file_location("ingest_main_meals", _PATH)
ingest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest)

TZ = ZoneInfo("Europe/Lisbon")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=TZ)
TODAY = NOW.date().isoformat()
TWO_DAYS_AGO = (NOW - timedelta(days=2)).date().isoformat()
HDR = {"X-Auth-Token": "t"}


class _Frozen(datetime):
    """`datetime` with a fixed `now`, so "today" and "closed day" are stable."""

    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheet:
    """The subset of the Sheets API the meal paths use, applied to real grids."""

    def __init__(self, tabs):
        self.tabs = {name: [list(r) for r in grid] for name, grid in tabs.items()}
        self.ids = {name: n for n, name in enumerate(self.tabs, start=7)}
        self.calls = []
        self._values = False
        self._pending = None

    # chain
    def spreadsheets(self):
        self._values = False
        return self

    def values(self):
        self._values = True
        return self

    def batchUpdate(self, spreadsheetId, body):
        self._pending = ("values" if self._values else "sheet", body)
        return self

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        self._pending = ("append", (range.split("!")[0], body["values"]))
        return self

    def get(self, **kw):  # _ensure_meals_tab's header check
        self._pending = ("get", kw)
        return self

    def execute(self):
        kind, payload = self._pending
        self.calls.append(kind)
        if kind == "values":
            for entry in payload["data"]:
                tab, cell = entry["range"].split("!")
                m = re.match(r"([A-Z]+)(\d+)", cell)
                col, row = _col_index(m.group(1)), int(m.group(2)) - 1
                grid = self.tabs[tab]
                while len(grid) <= row:
                    grid.append([])
                for offset, value in enumerate(entry["values"][0]):
                    target = grid[row]
                    while len(target) <= col + offset:
                        target.append("")
                    target[col + offset] = value
        elif kind == "append":
            tab, rows = payload
            self.tabs[tab].extend(list(r) for r in rows)
        elif kind == "sheet":
            by_id = {v: k for k, v in self.ids.items()}
            for request in payload["requests"]:
                if "deleteDimension" in request:
                    rng = request["deleteDimension"]["range"]
                    del self.tabs[by_id[rng["sheetId"]]][rng["startIndex"]:rng["endIndex"]]
                elif "sortRange" in request:
                    grid = self.tabs[by_id[request["sortRange"]["range"]["sheetId"]]]
                    grid[1:] = sorted(grid[1:], key=lambda r: str(r[0]))
        return {}

    def meals(self):
        header, *rows = self.tabs["meals"]
        return [dict(zip(header, r)) for r in rows]


def _row(when, items, *, model="claude-sonnet-5", sha="sha1", photo="http://p",
         note="n", edited=""):
    items = ingest._normalize_items(items)
    totals = ingest._meal_totals(items)
    return [when, ", ".join(i["name"] for i in items), json.dumps(items),
            totals["calories"], totals["protein_g"], totals["carbs_g"],
            totals["fat_g"], 0.6, model, photo, totals["portion_g"], sha, note,
            "", edited]


OATS = {"name": "rolled oats", "name_pt": "aveia", "portion_g": 50, "calories": 190,
        "protein_g": 6.5, "carbs_g": 33, "fat_g": 3.5,
        "nutrients": {"fiber_g": 5.0, "iron_mg": 2.2}}
WHEY = {"name": "whey protein", "portion_g": 20, "calories": 78, "protein_g": 16,
        "carbs_g": 1.5, "fat_g": 1.2, "nutrients": {"calcium_mg": 120.0}}
BREAKFAST_ID = f"{TODAY}T09:30:00+01:00"
OLD_ID = f"{TWO_DAYS_AGO}T09:30:00+01:00"


@pytest.fixture
def api(monkeypatch):
    sheet = FakeSheet({
        "meals": [ingest.MEALS_HEADERS,
                  _row(OLD_ID, [OATS, WHEY], sha="old"),
                  _row(BREAKFAST_ID, [OATS, WHEY], sha="today")],
        "daily_summary": [["date", "total_cals_out", "total_cals_in",
                           "total_protein_g", "total_carbs_g", "total_fat_g",
                           "total_fiber_g", "total_iron_mg", "total_calcium_mg",
                           "energy_balance_kcal"],
                          [TWO_DAYS_AGO, 2600, 268, 22.5, 34.5, 4.7, 5.0, 2.2, 120,
                           -2332]],
    })
    queued, coach = [], []
    monkeypatch.setattr(ingest, "datetime", _Frozen)
    monkeypatch.setattr(ingest, "_sheets", lambda: sheet)
    monkeypatch.setattr(ingest, "_sid", lambda: "sid")
    monkeypatch.setattr(ingest, "_read_tab",
                        lambda tab: [list(r) for r in sheet.tabs.get(tab, [])])
    monkeypatch.setattr(ingest, "_tab_id", lambda tab: sheet.ids.get(tab))
    monkeypatch.setattr(ingest, "_ensure_meals_tab", lambda: sheet.ids["meals"])
    monkeypatch.setattr(ingest, "_display_taxonomy", lambda: None)
    monkeypatch.setattr(ingest, "_enqueue_process", queued.append)
    monkeypatch.setattr(ingest, "_trigger_coach_refresh", coach.append)
    # the registry's nutrition block, narrowed to the columns this fake day has
    monkeypatch.setattr(ingest, "names_in", lambda block: [
        "energy_balance_kcal", "total_cals_in", "total_protein_g", "total_carbs_g",
        "total_fat_g", "total_fiber_g", "total_iron_mg", "total_calcium_mg"])
    monkeypatch.setenv("INGEST_TOKEN", "t")
    client = ingest.app.test_client()
    client.sheet, client.queued, client.coach = sheet, queued, coach
    return client


def _served(api, meal_id=BREAKFAST_ID):
    """A meal exactly as /today serves it — what the app edits and sends back."""
    rows = [m for m in api.sheet.meals() if m["datetime"] == meal_id]
    return ingest._today_meals_out(rows)[0]


def _entries(meal, **portions):
    return [{"base": item, **({"portion_g": portions[item["key"]]}
                              if item["key"] in portions else {})}
            for item in meal["items"]]


def _stored(api, meal_id):
    return next(m for m in api.sheet.meals() if m["datetime"] == meal_id)


# -- auth ----------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", [("post", "/meals/save"),
                                         ("post", "/meals/delete"),
                                         ("get", "/meals/library")])
def test_every_meal_endpoint_requires_the_token(api, method, path):
    assert getattr(api, method)(path, json={}).status_code == 401


# -- editing an existing meal ---------------------------------------------------------
def test_a_new_portion_rescales_every_number_of_that_item(api):
    meal = _served(api)
    r = api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "rev": meal["rev"],
        "items": _entries(meal, **{"rolled oats": 25})})
    assert r.status_code == 200

    row = _stored(api, BREAKFAST_ID)
    oats, whey = json.loads(row["items"])
    assert oats["portion_g"] == 25 and oats["calories"] == 95
    assert oats["nutrients"] == {"fiber_g": 2.5, "iron_mg": 1.1}   # the micros too
    assert oats["name"] == "rolled oats" and oats["name_pt"] == "aveia"
    assert whey == ingest._normalize_items([WHEY])[0]               # untouched
    assert row["calories"] == 95 + 78 and row["portion_g"] == 45
    assert row["edited_at"]
    # provenance is never rewritten by an edit
    assert (row["model"], row["image_sha"], row["photo_url"], row["note"]) == \
        ("claude-sonnet-5", "today", "http://p", "n")
    assert r.get_json()["calories"] == 173
    assert api.coach == ["meal_logged"]      # today changed: the coach is stale


def test_removing_an_ingredient_updates_the_food_line_and_totals(api):
    meal = _served(api)
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(meal)[:1]})
    row = _stored(api, BREAKFAST_ID)
    assert row["foods"] == "rolled oats" and row["calories"] == 190


def test_an_override_corrects_macros_and_keeps_the_micros(api):
    meal = _served(api)
    entries = _entries(meal)
    entries[1]["override"] = {"protein_g": 14}
    api.post("/meals/save", headers=HDR, json={"datetime": BREAKFAST_ID,
                                               "items": entries})
    whey = json.loads(_stored(api, BREAKFAST_ID)["items"])[1]
    assert whey["protein_g"] == 14 and whey["nutrients"] == {"calcium_mg": 120.0}


def test_an_ingredient_from_the_library_is_added_at_its_own_grams(api):
    meal = _served(api)
    banana = ingest._display_items(ingest._normalize_items([{
        "name": "banana", "portion_g": 120, "calories": 107, "protein_g": 1.3,
        "carbs_g": 27, "fat_g": 0.4, "nutrients": {"potassium_mg": 430.0}}]), None)[0]
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID,
        "items": _entries(meal) + [{"base": banana, "portion_g": 60}]})
    added = json.loads(_stored(api, BREAKFAST_ID)["items"])[-1]
    assert added["portion_g"] == 60 and added["nutrients"] == {"potassium_mg": 215.0}


def test_a_stale_edit_is_refused_not_applied(api):
    before = _stored(api, BREAKFAST_ID)
    r = api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "rev": "stale",
        "items": _entries(_served(api), **{"rolled oats": 80})})
    assert r.status_code == 409 and r.get_json()["stale"] is True
    assert _stored(api, BREAKFAST_ID) == before


def test_a_retried_edit_that_already_landed_is_not_a_conflict(api):
    meal = _served(api)
    payload = {"datetime": BREAKFAST_ID, "rev": meal["rev"], "describe": "mel",
               "items": _entries(meal, **{"rolled oats": 40})}
    first = api.post("/meals/save", headers=HDR, json=payload)
    retry = api.post("/meals/save", headers=HDR, json=payload)   # stale rev now
    assert first.status_code == retry.status_code == 200
    names = [i["name"] for i in json.loads(_stored(api, BREAKFAST_ID)["items"])]
    assert names == ["rolled oats", "whey protein", "mel"]      # one placeholder
    assert len(api.queued) == 1                                 # one estimate


@pytest.mark.parametrize("payload,status", [
    ({"datetime": "2099-01-01T00:00:00+01:00", "items": []}, 404),
    ({"datetime": BREAKFAST_ID, "items": []}, 400),            # delete instead
    ({"datetime": BREAKFAST_ID, "items": "nope"}, 400),
    ({"datetime": BREAKFAST_ID, "items": [{"portion_g": 5}]}, 400),
])
def test_bad_edits_are_rejected_without_writing(api, payload, status):
    assert api.post("/meals/save", headers=HDR, json=payload).status_code == status
    assert "values" not in api.sheet.calls


def test_zero_grams_is_rejected(api):
    meal = _served(api)
    r = api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(meal, **{"whey protein": 0})})
    assert r.status_code == 400


# -- a closed day is re-totalled at once ---------------------------------------------
def test_editing_a_closed_day_rewrites_its_nutrition_columns(api):
    meal = _served(api, OLD_ID)
    api.post("/meals/save", headers=HDR, json={
        "datetime": OLD_ID, "items": _entries(meal)[:1]})   # the whey is gone

    header, row = api.sheet.tabs["daily_summary"][0], api.sheet.tabs["daily_summary"][1]
    day = dict(zip(header, row))
    assert day["total_cals_in"] == 190
    assert day["total_protein_g"] == 6.5
    assert day["total_calcium_mg"] == ""       # the whey's calcium left with it
    assert day["energy_balance_kcal"] == 190 - 2600
    assert day["total_cals_out"] == 2600       # another source's column: untouched
    assert api.coach == []                      # not today: the coach doesn't care


def test_editing_today_leaves_daily_summary_alone(api):
    before = [list(r) for r in api.sheet.tabs["daily_summary"]]
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api))[:1]})
    assert api.sheet.tabs["daily_summary"] == before


# -- repeating a meal -------------------------------------------------------------------
def test_repeating_a_meal_logs_it_instantly_with_its_own_numbers(api):
    source = _served(api, OLD_ID)
    r = api.post("/meals/save", headers=HDR, json={
        "client_id": "abc-123", "time": "11:15", "confidence": source["confidence"],
        "items": _entries(source, **{"whey protein": 10})})
    assert r.status_code == 200
    new_id = f"{TODAY}T11:15:00+01:00"
    assert r.get_json()["datetime"] == new_id

    row = _stored(api, new_id)
    assert row["model"] == ingest.APP_MODEL and row["image_sha"] == "app:abc-123"
    assert row["photo_url"] == "" and not row.get("edited_at")
    assert row["confidence"] == 0.6
    whey = json.loads(row["items"])[1]
    assert whey["portion_g"] == 10 and whey["nutrients"] == {"calcium_mg": 60.0}
    assert row["calories"] == 190 + 39
    assert api.queued == []                    # nothing for a model to do
    assert api.coach == ["meal_logged"]


def test_a_retried_save_of_a_new_meal_does_not_log_it_twice(api):
    payload = {"client_id": "same", "time": "11:15",
               "items": _entries(_served(api, OLD_ID))}
    first = api.post("/meals/save", headers=HDR, json=payload).get_json()
    second = api.post("/meals/save", headers=HDR, json=payload).get_json()
    assert first["datetime"] == second["datetime"]
    assert sum(m["image_sha"] == "app:same" for m in api.sheet.meals()) == 1


def test_two_meals_at_the_same_minute_stay_two_meals(api):
    entries = _entries(_served(api, OLD_ID))
    ids = [api.post("/meals/save", headers=HDR, json={
        "client_id": cid, "time": "09:30", "items": entries}).get_json()["datetime"]
        for cid in ("a", "b")]
    assert ids == [f"{TODAY}T09:30:01+01:00", f"{TODAY}T09:30:02+01:00"]


def test_a_meal_can_be_logged_on_an_earlier_day(api):
    r = api.post("/meals/save", headers=HDR, json={
        "client_id": "x", "date": TWO_DAYS_AGO, "time": "20:00",
        "items": _entries(_served(api))})
    assert r.get_json()["datetime"] == f"{TWO_DAYS_AGO}T20:00:00+01:00"
    day = dict(zip(*api.sheet.tabs["daily_summary"][:2]))
    assert day["total_cals_in"] == 2 * 268     # the closed day now counts it


@pytest.mark.parametrize("extra,why", [
    ({"client_id": "x", "time": "12:30"}, "future"),
    ({"client_id": "x", "time": "9:30"}, "bad time"),
    ({"client_id": "x", "date": "24/09/2026"}, "bad date"),
    ({"time": "11:00"}, "no client_id"),
])
def test_a_new_meal_needs_an_id_and_a_real_past_time(api, extra, why):
    r = api.post("/meals/save", headers=HDR, json={
        "items": _entries(_served(api)), **extra})
    assert r.status_code == 400, why


def test_a_placeholder_cannot_be_smuggled_into_a_new_meal(api):
    ghost = ingest._display_items([{"name": "x", "portion_g": 0, "calories": 0,
                                    "protein_g": 0, "carbs_g": 0, "fat_g": 0,
                                    "status": "pending"}], None)[0]
    r = api.post("/meals/save", headers=HDR, json={
        "client_id": "x", "time": "11:00", "items": [{"base": ghost}]})
    assert r.status_code == 400


# -- describing an ingredient nobody has logged before ----------------------------------
def _estimate(api, monkeypatch, job, items, *, attempt=0, kind="meal"):
    monkeypatch.setattr(ingest, "analyze_text", lambda *a, **k: {
        "kind": kind, "items": ingest._normalize_items(items)})
    return api.post("/process", json=job, headers={
        **HDR, "X-CloudTasks-TaskRetryCount": str(attempt)})


def test_a_described_ingredient_is_saved_now_and_estimated_later(api, monkeypatch):
    r = api.post("/meals/save", headers=HDR, json={
        "client_id": "d1", "time": "11:00", "items": _entries(_served(api)),
        "describe": "  banana   120 g "})
    meal_id = r.get_json()["datetime"]
    items = json.loads(_stored(api, meal_id)["items"])
    assert items[-1] == {"name": "banana 120 g", "portion_g": 0.0, "calories": 0.0,
                         "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0,
                         "status": "pending"}
    job = api.queued[0]
    assert job["merge_into"] == meal_id and job["placeholder"] == "banana 120 g"

    banana = {"name": "banana", "portion_g": 120, "calories": 107, "protein_g": 1.3,
              "carbs_g": 27, "fat_g": 0.4, "meal_time": "11:00"}
    r = _estimate(api, monkeypatch, job, [banana])
    assert r.get_json()["status"] == "merged"
    row = _stored(api, meal_id)
    items = json.loads(row["items"])
    assert items[-1]["name"] == "banana" and "meal_time" not in items[-1]
    assert not any(ingest.meal_library.is_placeholder(i) for i in items)
    assert row["calories"] == 190 + 78 + 107

    # a queue retry after the merge landed must not add the banana twice
    assert _estimate(api, monkeypatch, job, [banana]).get_json()["status"] == "gone"
    assert len(json.loads(_stored(api, meal_id)["items"])) == 3


def test_a_meal_of_only_a_description_is_listed_while_it_waits(api):
    r = api.post("/meals/save", headers=HDR, json={
        "client_id": "d2", "time": "11:00", "items": [], "describe": "tosta mista"})
    assert r.status_code == 200
    listed = ingest._today_meals_out(api.sheet.meals())
    assert any(m["datetime"] == r.get_json()["datetime"] for m in listed)


def test_an_estimate_that_never_comes_marks_the_ingredient_failed(api, monkeypatch):
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api)),
        "describe": "mel 10 g"})
    job = api.queued[0]

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(ingest, "analyze_text", boom)
    retry = api.post("/process", json=job, headers={**HDR,
                                                    "X-CloudTasks-TaskRetryCount": "0"})
    assert retry.status_code == 500                      # the queue will try again
    last = api.post("/process", json=job, headers={
        **HDR, "X-CloudTasks-TaskRetryCount": str(ingest._max_attempts() - 1)})
    assert last.status_code == 200 and last.get_json()["status"] == "failed"
    assert json.loads(_stored(api, BREAKFAST_ID)["items"])[-1]["status"] == "failed"


def test_a_note_that_is_not_food_fails_the_ingredient(api, monkeypatch):
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api)), "describe": "xyz"})
    r = _estimate(api, monkeypatch, api.queued[0], [], kind="bowel")
    assert r.get_json()["status"] == "failed"


def test_an_unreachable_queue_fails_the_ingredient_at_once(api, monkeypatch):
    def down(payload):
        raise ConnectionError("queue down")
    monkeypatch.setattr(ingest, "_enqueue_process", down)
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api)),
        "describe": "mel 10 g"})
    assert json.loads(_stored(api, BREAKFAST_ID)["items"])[-1]["status"] == "failed"


def test_an_edit_keeps_a_live_placeholder_and_drops_a_stale_one(api, monkeypatch):
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api)), "describe": "mel"})
    served = _served(api)
    assert served["items"][-1]["status"] == "pending"
    stale = dict(served["items"][-1], key="compota", name="compota")
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID,
        "items": _entries(served) + [{"base": stale}]})
    names = [i["name"] for i in json.loads(_stored(api, BREAKFAST_ID)["items"])]
    assert names == ["rolled oats", "whey protein", "mel"]


def test_removing_a_pending_ingredient_cancels_its_estimate(api, monkeypatch):
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api)), "describe": "mel"})
    job = api.queued[0]
    api.post("/meals/save", headers=HDR, json={
        "datetime": BREAKFAST_ID, "items": _entries(_served(api))[:2]})
    r = _estimate(api, monkeypatch, job, [{"name": "honey", "portion_g": 10,
                                           "calories": 30}])
    assert r.get_json()["status"] == "gone"
    assert len(json.loads(_stored(api, BREAKFAST_ID)["items"])) == 2


# -- deleting --------------------------------------------------------------------------
def test_delete_removes_exactly_that_meal_and_is_idempotent(api):
    r = api.post("/meals/delete", headers=HDR, json={"datetime": BREAKFAST_ID})
    assert r.get_json() == {"deleted": True, "datetime": BREAKFAST_ID}
    assert [m["datetime"] for m in api.sheet.meals()] == [OLD_ID]
    again = api.post("/meals/delete", headers=HDR, json={"datetime": BREAKFAST_ID})
    assert again.status_code == 200 and again.get_json()["deleted"] is False


def test_deleting_the_last_meal_of_a_closed_day_blanks_its_totals(api):
    api.post("/meals/delete", headers=HDR, json={"datetime": OLD_ID})
    day = dict(zip(*api.sheet.tabs["daily_summary"][:2]))
    assert day["total_cals_in"] == "" and day["total_iron_mg"] == ""
    assert day["energy_balance_kcal"] == ""
    assert day["total_cals_out"] == 2600


def test_delete_needs_a_datetime(api):
    assert api.post("/meals/delete", headers=HDR, json={}).status_code == 400


# -- what the app is served ------------------------------------------------------------
def test_served_items_carry_their_english_key_and_meals_their_rev(api):
    meal = _served(api)
    assert [(i["name"], i["key"]) for i in meal["items"]] == [
        ("aveia", "rolled oats"), ("whey protein", "whey protein")]
    assert meal["rev"] == ingest._meal_rev(_stored(api, BREAKFAST_ID))
    assert "template" not in meal


def test_the_library_offers_habits_recent_meals_and_ingredients(api):
    body = api.get("/meals/library", headers=HDR).get_json()
    habit = body["suggestions"][0]
    assert habit["count"] == 2 and habit["meal"]["datetime"] == BREAKFAST_ID
    assert habit["id"] == BREAKFAST_ID and habit["typical_time"] == "09:30"
    assert [m["datetime"] for m in body["recent"]] == [BREAKFAST_ID, OLD_ID]
    by_key = {e["key"]: e for e in body["ingredients"]}
    assert by_key["rolled oat"]["count"] == 2
    assert by_key["rolled oat"]["item"]["name"] == "aveia"
    assert by_key["rolled oat"]["item"]["key"] == "rolled oats"
