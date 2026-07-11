"""Minimal client for the Google Health API v4.

Docs: https://developers.google.com/health/reference/rest
Read pattern: GET /v4/users/{user}/dataTypes/{dataType}/dataPoints

The token used here must carry ONLY Google Health scopes — the API rejects any
token that also holds a Drive scope (403 DISALLOWED_OAUTH_SCOPES).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
from google.oauth2.credentials import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://health.googleapis.com/v4"

# Known data type identifiers (from the API discovery document).
WEIGHT = "weight"
BODY_FAT = "body-fat"


def _session_with_retries() -> requests.Session:
    """GETs are safe to retry; back off on rate limits and transient 5xx."""
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class GoogleHealthClient:
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._session = _session_with_retries()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._creds.token}"}

    def list_data_points(
        self,
        data_type: str,
        *,
        user: str = "me",
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return all data points for a data type, following pagination.

        start_time / end_time are optional RFC3339 timestamps. They are only
        sent when provided; callers should still filter client-side, since day
        attribution here is by *local* civil day, not UTC.
        """
        url = f"{BASE_URL}/users/{user}/dataTypes/{data_type}/dataPoints"
        params: Dict[str, Any] = {"pageSize": page_size}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        points: List[Dict[str, Any]] = []
        while True:
            resp = self._session.get(
                url, headers=self._headers(), params=params, timeout=30
            )
            resp.raise_for_status()
            body = resp.json()
            points.extend(body.get("dataPoints", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token
        return points
