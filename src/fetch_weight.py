"""Step 1: fetch weight + body-fat from the Google Health API and save to a file.

    python -m src.fetch_weight

On the first run it opens a browser for consent; after that it runs silently.
Raw responses are written to data/bronze/ (our immutable landing zone), and the
latest reading is printed to the console.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.auth import get_credentials
from src.google_health import BODY_FAT, WEIGHT, GoogleHealthClient

BASE_DIR = Path(__file__).resolve().parent.parent
BRONZE_DIR = BASE_DIR / "data" / "bronze"

# In the API response, each data point nests its measurement under a type key.
METRIC_KEY = {WEIGHT: "weight", BODY_FAT: "bodyFat"}


def _sample_time(point: Dict[str, Any], metric_key: str) -> str:
    """RFC3339 timestamp of the reading (UTC 'Z'), or '' if missing."""
    return (
        point.get(metric_key, {})
        .get("sampleTime", {})
        .get("physicalTime", "")
    )


def _latest(points: List[Dict[str, Any]], metric_key: str) -> Optional[Dict[str, Any]]:
    # physicalTime is UTC (Z), so lexicographic sort == chronological.
    return max(points, key=lambda p: _sample_time(p, metric_key), default=None)


def _normalize_weight(point: Dict[str, Any]) -> Dict[str, Any]:
    m = point.get("weight", {})
    grams = m.get("weightGrams")
    ts = _sample_time(point, "weight")
    return {
        "date": ts[:10],
        "time": ts,
        "weight_kg": round(grams / 1000, 2) if isinstance(grams, (int, float)) else None,
        "platform": point.get("dataSource", {}).get("platform"),
    }


def _normalize_fat(point: Dict[str, Any]) -> Dict[str, Any]:
    m = point.get("bodyFat", {})
    ts = _sample_time(point, "bodyFat")
    return {
        "date": ts[:10],
        "time": ts,
        "body_fat_pct": m.get("percentage"),
        "platform": point.get("dataSource", {}).get("platform"),
    }


def main() -> None:
    creds = get_credentials(interactive=True)
    client = GoogleHealthClient(creds)

    weight_points = client.list_data_points(WEIGHT)
    fat_points = client.list_data_points(BODY_FAT)

    weights = sorted(
        (_normalize_weight(p) for p in weight_points),
        key=lambda r: r["time"],
        reverse=True,
    )
    fats = sorted(
        (_normalize_fat(p) for p in fat_points),
        key=lambda r: r["time"],
        reverse=True,
    )

    BRONZE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = BRONZE_DIR / f"weight_{stamp}.json"
    raw_path.write_text(
        json.dumps(
            {
                "fetched_at": stamp,
                "source_api": "google-health-api/v4",
                "normalized": {"weight": weights, "body_fat": fats},
                "raw": {"weight": weight_points, "body_fat": fat_points},
            },
            indent=2,
        )
    )

    print(f"Weight readings:   {len(weights)}")
    print(f"Body-fat readings: {len(fats)}")

    if weights:
        w = weights[0]
        print(f"\nLatest weight:   {w['weight_kg']} kg  @ {w['time']}  ({w['platform']})")
    else:
        print("\nNo weight data yet — weigh in and let the app sync.")

    if fats:
        f = fats[0]
        print(f"Latest body-fat: {f['body_fat_pct']}%  @ {f['time']}  ({f['platform']})")

    if weights:
        print("\nRecent weigh-ins:")
        for w in weights[:5]:
            print(f"  {w['date']}   {w['weight_kg']} kg")

    print(f"\nRaw + normalized data saved to: {raw_path}")


if __name__ == "__main__":
    main()
