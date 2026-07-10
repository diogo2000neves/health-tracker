"""Minimal client for the Google Health API v4.

Docs: https://developers.google.com/health/reference/rest
Read pattern: GET /v4/users/{user}/dataTypes/{dataType}/dataPoints
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
from google.oauth2.credentials import Credentials

BASE_URL = "https://health.googleapis.com/v4"

# Known data type identifiers (from the API discovery document).
WEIGHT = "weight"
BODY_FAT = "body-fat"


class GoogleHealthClient:
    def __init__(self, creds: Credentials):
        self._creds = creds
        self._session = requests.Session()

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
        sent when provided, so the default call is a safe "fetch recent" that
        we filter client-side.
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
