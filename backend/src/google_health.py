"""Minimal client for the Google Health API v4 (read-only).

Docs: https://developers.google.com/health/reference/rest
Contract verified against the discovery doc (rev 20260715), not assumed — the
previous incarnation of this file guessed and was wrong (see FILTERS below).

Two endpoints matter:

* ``list``        GET  /v4/users/me/dataTypes/{type}/dataPoints
* ``dailyRollUp`` POST /v4/users/me/dataTypes/{type}/dataPoints:dailyRollUp

**Use dailyRollUp wherever it is supported.** Intraday types (steps, distance,
heart rate…) arrive as hundreds of one-minute buckets; dailyRollUp aggregates them
server-side over *civil* time, which is exactly the local-day grain this project
keys everything on. It is also the only way to reach some data: ``floors`` has no
``list`` endpoint at all (400: "List is not supported for data type floors"), and
``total-calories`` — calories *out*, the other half of the energy balance — exists
only as a rollup.

The token used here must carry ONLY Google Health scopes: the API rejects any
token that also holds a Drive scope (403 DISALLOWED_OAUTH_SCOPES). See src/auth.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests
from google.oauth2.credentials import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://health.googleapis.com/v4"

# -- data types ---------------------------------------------------------------
# Daily summaries: one point per day, already keyed on a civil `date` field.
DAILY_RESTING_HR = "daily-resting-heart-rate"
DAILY_HRV = "daily-heart-rate-variability"
DAILY_SPO2 = "daily-oxygen-saturation"
DAILY_RESPIRATORY = "daily-respiratory-rate"
DAILY_SKIN_TEMP = "daily-sleep-temperature-derivations"

DAILY_TYPES = [
    DAILY_RESTING_HR, DAILY_HRV, DAILY_SPO2, DAILY_RESPIRATORY, DAILY_SKIN_TEMP,
]

# Sleep sessions (one per night, plus naps — see biometrics.daily_sleep).
SLEEP = "sleep"

# Types pulled via dailyRollUp. Verified populated for this user's tracker; the
# ones that returned nothing (floors, altitude, vo2-max, exercise) are simply not
# produced by the device and are left out rather than fetched for no reason.
ROLLUP_TYPES = [
    "steps", "distance", "total-calories", "active-energy-burned",
    "active-minutes", "active-zone-minutes", "sedentary-period", "heart-rate",
    "time-in-heart-rate-zone", "swim-lengths-data",
]

# `list` filter expression per data type family. Getting these wrong is silent:
# the API 400s and the old code caught that and retried *unbounded*, quietly
# pulling everything. Patterns come straight from the discovery doc.
#   daily summaries -> `{type}.date`
#   sleep           -> `sleep.interval.civil_end_time`  (the WAKE day — the API's
#                      own convention, and the one the sheet uses)
FILTERS = {
    "daily": '{t}.date >= "{start}" AND {t}.date < "{end}"',
    "sleep": 'sleep.interval.civil_end_time >= "{start}" '
             'AND sleep.interval.civil_end_time < "{end}"',
}

# sleep/exercise cap at 25; everything else defaults to 1440 (max 10000).
_SLEEP_PAGE_SIZE = 25
_PAGE_SIZE = 1000

# dailyRollUp refuses a range longer than this, per data type (from the discovery
# doc's `range` field). Exceeding it is a flat 400 — and the 14-day group is
# exactly where `total-calories` lives, so a backfill would lose calories-out
# rather than fail loudly. Requests are chunked to respect it.
_ROLLUP_MAX_DAYS = {
    "calories-in-heart-rate-zone": 14,
    "heart-rate": 14,
    "active-minutes": 14,
    "total-calories": 14,
}
_ROLLUP_DEFAULT_MAX_DAYS = 90


def _snake(data_type: str) -> str:
    """`daily-resting-heart-rate` -> `daily_resting_heart_rate` (filters use snake
    case, the URL path uses kebab)."""
    return data_type.replace("-", "_")


def _session_with_retries() -> requests.Session:
    """Back off on rate limits and transient 5xx. POST is included because
    dailyRollUp is a POST but semantically a read — it is safe to retry."""
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _civil(day: date) -> Dict[str, Any]:
    return {"date": {"year": day.year, "month": day.month, "day": day.day},
            "time": {"hours": 0, "minutes": 0}}


class GoogleHealthClient:
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._session = _session_with_retries()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._creds.token}"}

    # -- list -----------------------------------------------------------------
    def list_data_points(
        self,
        data_type: str,
        *,
        family: str = "daily",
        start: Optional[str] = None,
        end: Optional[str] = None,
        user: str = "me",
    ) -> List[Dict[str, Any]]:
        """Data points for `data_type`, following pagination.

        `family` picks the filter shape ("daily" or "sleep"). The filter is only
        sent when both bounds are given; an unfiltered call pulls the full history.
        """
        url = f"{BASE_URL}/users/{user}/dataTypes/{data_type}/dataPoints"
        params: Dict[str, Any] = {
            "pageSize": _SLEEP_PAGE_SIZE if data_type == SLEEP else _PAGE_SIZE,
        }
        if start and end:
            params["filter"] = FILTERS[family].format(
                t=_snake(data_type), start=start, end=end)

        points: List[Dict[str, Any]] = []
        while True:
            resp = self._session.get(url, headers=self._headers(), params=params,
                                     timeout=30)
            resp.raise_for_status()
            body = resp.json()
            points.extend(body.get("dataPoints", []))
            token = body.get("nextPageToken")
            if not token:
                return points
            params["pageToken"] = token

    # -- dailyRollUp -----------------------------------------------------------
    def _rollup_chunk(self, url: str, start: date, end: date) -> List[Dict[str, Any]]:
        """One dailyRollUp request for a range known to be within the type's cap.

        `pageSize` is deliberately not sent: the API 400s on values it documents as
        legal (100 is rejected for a 9-day range, 10 is accepted) and the default
        of 1440 points already dwarfs the one-point-per-day this returns.
        """
        body: Dict[str, Any] = {
            "range": {"start": _civil(start), "end": _civil(end)},
            "windowSizeDays": 1,
        }
        points: List[Dict[str, Any]] = []
        while True:
            resp = self._session.post(url, headers=self._headers(), json=body,
                                      timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            points.extend(payload.get("rollupDataPoints", []))
            token = payload.get("nextPageToken")
            if not token:
                return points
            body["pageToken"] = token

    def daily_rollup(
        self,
        data_type: str,
        start: date,
        end: date,
        *,
        user: str = "me",
    ) -> List[Dict[str, Any]]:
        """Server-side per-civil-day aggregation over [start, end).

        Automatically split into chunks the API will accept (see _ROLLUP_MAX_DAYS),
        so a long backfill works instead of 400ing. Returns the raw rollup points:
        each carries `civilStartTime` plus a single value object named after the
        data type. A type the device never produced comes back empty.
        """
        url = (f"{BASE_URL}/users/{user}/dataTypes/{data_type}"
               f"/dataPoints:dailyRollUp")
        span = _ROLLUP_MAX_DAYS.get(data_type, _ROLLUP_DEFAULT_MAX_DAYS)
        points: List[Dict[str, Any]] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=span), end)
            points.extend(self._rollup_chunk(url, chunk_start, chunk_end))
            chunk_start = chunk_end
        return points
