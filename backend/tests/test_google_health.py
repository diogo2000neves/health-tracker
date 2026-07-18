"""Unit tests for the Google Health client's request shaping (no network).

These pin the two contracts the API enforces but does not forgive: the filter
expression and the dailyRollUp range cap. Both fail as a flat 400, and the
previous incarnation of this client caught that and silently retried unbounded.
"""
from datetime import date

from src.google_health import DAILY_TYPES, FILTERS, ROLLUP_TYPES, SLEEP, GoogleHealthClient


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Records every request instead of sending it."""

    def __init__(self):
        self.gets = []
        self.posts = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({"url": url, "params": params})
        return _FakeResp({"dataPoints": []})

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "body": json})
        return _FakeResp({"rollupDataPoints": []})


def _client():
    class _Creds:
        token = "t"

    client = GoogleHealthClient(_Creds())
    client._session = _FakeSession()
    return client


# -- filters -------------------------------------------------------------------
def test_sleep_is_filtered_on_the_wake_day():
    # sleep spans midnight, so it must be keyed on when it ENDED — the API's own
    # convention, and the one daily_summary uses.
    client = _client()
    client.list_data_points(SLEEP, family="sleep", start="2026-07-08",
                            end="2026-07-17")
    flt = client._session.gets[0]["params"]["filter"]
    assert flt == ('sleep.interval.civil_end_time >= "2026-07-08" '
                   'AND sleep.interval.civil_end_time < "2026-07-17"')
    assert client._session.gets[0]["params"]["pageSize"] == 25  # sleep caps at 25


def test_daily_types_filter_on_their_civil_date_in_snake_case():
    client = _client()
    client.list_data_points("daily-heart-rate-variability", family="daily",
                            start="2026-07-08", end="2026-07-17")
    flt = client._session.gets[0]["params"]["filter"]
    # the URL path is kebab-case but the filter field is snake_case
    assert flt == ('daily_heart_rate_variability.date >= "2026-07-08" '
                   'AND daily_heart_rate_variability.date < "2026-07-17"')
    assert "daily-heart-rate-variability" in client._session.gets[0]["url"]


def test_no_filter_is_sent_without_both_bounds():
    client = _client()
    client.list_data_points("daily-resting-heart-rate", family="daily")
    assert "filter" not in client._session.gets[0]["params"]


# -- dailyRollUp range cap ------------------------------------------------------
def test_rollup_chunks_the_14_day_types():
    # total-calories (calories OUT) caps at 14 days; a 30-day backfill must be
    # split, not 400. This is the failure a 7-day default window would never
    # surface until someone ran a real backfill.
    client = _client()
    client.daily_rollup("total-calories", date(2026, 6, 1), date(2026, 7, 1))
    ranges = [p["body"]["range"] for p in client._session.posts]
    assert len(ranges) == 3                      # 14 + 14 + 2
    starts = [r["start"]["date"]["day"] for r in ranges]
    assert starts == [1, 15, 29]
    # contiguous and closed-open: each chunk starts exactly where the last ended
    assert ranges[0]["end"]["date"] == ranges[1]["start"]["date"]
    assert ranges[-1]["end"]["date"] == {"year": 2026, "month": 7, "day": 1}


def test_rollup_sends_one_request_inside_the_cap():
    client = _client()
    client.daily_rollup("total-calories", date(2026, 7, 8), date(2026, 7, 17))
    assert len(client._session.posts) == 1


def test_rollup_uses_the_wider_cap_for_other_types():
    # 61 days: one request for steps (90-day cap), five for total-calories (14).
    span = (date(2026, 4, 1), date(2026, 6, 1))
    steps = _client()
    steps.daily_rollup("steps", *span)
    assert len(steps._session.posts) == 1

    calories = _client()
    calories.daily_rollup("total-calories", *span)
    assert len(calories._session.posts) == 5


def test_rollup_splits_exactly_at_the_cap_boundary():
    # 90 days is the last single-request range for a default-cap type; 91 splits.
    at_cap = _client()
    at_cap.daily_rollup("steps", date(2026, 4, 1), date(2026, 6, 30))
    assert len(at_cap._session.posts) == 1

    over = _client()
    over.daily_rollup("steps", date(2026, 4, 1), date(2026, 7, 1))
    assert len(over._session.posts) == 2


def test_rollup_never_sends_pagesize():
    # the API 400s on documented-legal pageSize values (100 rejected for a 9-day
    # range); the 1440 default already dwarfs one-point-per-day.
    client = _client()
    client.daily_rollup("steps", date(2026, 7, 8), date(2026, 7, 17))
    body = client._session.posts[0]["body"]
    assert "pageSize" not in body
    assert body["windowSizeDays"] == 1


# -- config sanity --------------------------------------------------------------
def test_the_14_day_types_are_all_in_the_rollup_set():
    from src.google_health import _ROLLUP_MAX_DAYS
    for capped in _ROLLUP_MAX_DAYS:
        # every capped type we actually fetch must be chunked; if one were fetched
        # without a cap entry it would 400 on a long backfill
        if capped in ROLLUP_TYPES:
            assert _ROLLUP_MAX_DAYS[capped] == 14


def test_data_type_sets_are_disjoint_and_populated():
    assert SLEEP not in ROLLUP_TYPES and SLEEP not in DAILY_TYPES
    assert not set(DAILY_TYPES) & set(ROLLUP_TYPES)
    assert "total-calories" in ROLLUP_TYPES      # calories out: rollup-only
    assert set(FILTERS) == {"daily", "sleep"}
