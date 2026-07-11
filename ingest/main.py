"""HTTP ingest service: meal photos + subjective-feel logging.

POST /ingest (X-Auth-Token) — meal photo in the body (raw or multipart):
  1. de-duplicates by image hash (double-taps don't double-log),
  2. estimates per-ingredient nutrition with Gemini (structured JSON output),
  3. archives the photo to the user's Google Drive,
  4. appends a row to the `meals` tab,
  5. replies with the meal summary plus the day's running totals.
  Non-food photos are archived but never logged as rows. If every model fails,
  the photo is archived and a zeroed "analysis failed" row keeps the audit
  trail — a meal is never silently lost.

POST /feel (X-Auth-Token) — {"score": 1-10[, "date": "YYYY-MM-DD"]} merges
  into daily_summary.subjective_feel ({"score": null} clears a mislog).

Auth model:
  * Gemini -> AI Studio key (billing-free project => free tier).
  * Sheets -> the runtime service account (Sheet is shared with it).
  * Drive  -> the *user's* OAuth token (service accounts have no Drive quota).

Clients and required env are initialised lazily so this module imports cleanly
in tests without credentials.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import io
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import google.auth
from flask import Flask, jsonify, request
from google import genai
from google.genai import types
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # reject absurd uploads

MEALS_TAB = "meals"
DAILY_TAB = "daily_summary"

# One row per meal. `items` is a JSON array breaking the plate into ingredients
# (each with its own portion + macros); the flat columns are the row totals the
# daily job rolls up. `image_sha` powers de-duplication. New columns are only
# ever appended at the end so previously written rows stay aligned.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "photo_url",
    "portion_g", "notes", "image_sha",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "L"

# Rows excluded from all totals (kept in sync with src/run_daily.py NON_MEALS).
NON_MEALS = {"not food", "analysis failed"}

# flash-lite first, deliberately: the bigger Flash models 503 on most free-tier
# calls (adding 10-40s of fallback latency), and a *consistent* estimator gives
# cleaner day-to-day trend deltas than a mix of models with different biases.
DEFAULT_MODELS = "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3-flash-preview"

PROMPT = """You are an expert nutritionist and food scientist doing computer-
vision meal analysis. Estimate every ingredient in the photo, its cooked weight
in grams, and its macros as accurately as possible. Being honest about
uncertainty matters more than giving confident round numbers.

Work through steps 1-5 IN ORDER inside the `reasoning` field FIRST, then fill in
`items`, `confidence` and `notes`. Do not skip the reasoning — thinking through
scale and hidden fats before committing to numbers is what makes them accurate.

1) CALIBRATE SCALE from whatever is actually in the photo.
Use any object that reveals real-world size — a plate/bowl, cutlery, a hand, a
can/bottle, packaging, a coin. Use only references genuinely present; NEVER
assume a specific item is there or is "standard". When you can confidently
identify a reference, use its typical size to calibrate (a dinner plate is
usually ~26-28 cm, a fork ~19 cm, a 330 ml can ~12 cm tall) — but only if you're
sure what the object is. Correct for camera angle (food shot at an angle looks
larger or smaller than top-down). If there is NO reliable reference, say so, fall
back to typical serving sizes, and lower your confidence.

2) FULL INVENTORY — including hidden ingredients.
List every visible component, even small ones (garnishes, seeds, herbs, cheese,
nuts, sauces, dressings). Then explicitly account for what is usually present but
NOT visible — this is the single largest source of calorie error, never skip it:
  - cooking oil/butter absorbed into or coating the food (anything fried,
    sauteed or roasted — estimate the fat, e.g. "pan-fried -> ~10 g oil"),
  - dressings, sauces or marinades soaked in,
  - added sugar, syrup or honey.

3) IDENTIFY EACH ITEM PRECISELY.
Commit to the most specific identification the image supports: exact food
("chicken thigh, skin-on" not "chicken"), fat level (full-fat vs low-fat dairy,
lean vs fatty cut) and cooking method (grilled/fried/boiled/raw/baked) — cooking
method changes both weight (water loss/absorption) and fat. Split composite
plates into separate items ("meat with rice" = two items). Distinguish
look-alikes by visual cues (tangerine vs orange, sweet potato vs potato, salmon
vs trout, prosciutto vs bacon, white vs brown rice). Name items in lowercase
singular English. If a packaged item shows a nutrition label, READ IT and scale
to the visible portion — labels beat estimation.

4) WEIGH EACH ITEM (cooked, as served).
Estimate each item's real edible weight in grams from its size in the frame and
its density (leafy greens are light per volume; meat, rice and stews are dense).
Include food partly hidden or layered behind other food — it still has mass.
Exclude inedible parts (peel, rind, bones, shells, stones). Do NOT default to
100 g and do NOT assume a standard serving.

5) COMPUTE MACROS PER ITEM.
For each item derive protein/carbs/fat for its estimated grams, then calories.
Sanity-check each: calories should be within ~10% of 4*protein + 4*carbs +
9*fat; fix the numbers if they disagree. Give PER-ITEM numbers only — do NOT sum
the meal yourself, the totals are computed automatically.

Rules:
- Never omit an ingredient because it is hard to quantify — estimate it and let
  it lower confidence instead of leaving it out.
- Caloric drinks (juice, soda, milk, beer) are items; water, plain tea and black
  coffee are ignored.
- If the image contains no food or drink, return items: [].
- confidence (0-1) reflects identification AND portion certainty for the whole
  meal; lower it when references are missing or items are ambiguous.
- notes: one short sentence — the scale reference you used (or that none was
  available) and the single assumption most likely to be wrong."""

# `reasoning` is generated FIRST (property ordering) so the model works through
# scale, hidden fats and portions before committing to numbers — that ordering
# is what improves accuracy. Meal totals are summed in code (see _meal_from_items),
# never by the model, to avoid arithmetic errors.
RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    property_ordering=["reasoning", "items", "confidence", "notes"],
    properties={
        "reasoning": types.Schema(type=types.Type.STRING),
        "items": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                property_ordering=["name", "cooking_method", "portion_g",
                                   "calories", "protein_g", "carbs_g", "fat_g"],
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    "cooking_method": types.Schema(type=types.Type.STRING),
                    "portion_g": types.Schema(type=types.Type.NUMBER),
                    "calories": types.Schema(type=types.Type.NUMBER),
                    "protein_g": types.Schema(type=types.Type.NUMBER),
                    "carbs_g": types.Schema(type=types.Type.NUMBER),
                    "fat_g": types.Schema(type=types.Type.NUMBER),
                },
                required=["name", "portion_g", "calories",
                          "protein_g", "carbs_g", "fat_g"],
            ),
        ),
        "confidence": types.Schema(type=types.Type.NUMBER),
        "notes": types.Schema(type=types.Type.STRING),
    },
    required=["reasoning", "items", "confidence", "notes"],
)


# -- lazy config / clients ----------------------------------------------------
def _models() -> List[str]:
    raw = os.environ.get("GEMINI_MODELS", DEFAULT_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))


def _sid() -> str:
    return os.environ["HEALTH_SPREADSHEET_ID"]


@functools.lru_cache(maxsize=1)
def _sheets():
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


@functools.lru_cache(maxsize=1)
def _drive():
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["HEALTH_OAUTH_TOKEN"])
    )
    if not creds.valid:
        creds.refresh(AuthRequest())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@functools.lru_cache(maxsize=1)
def _genai():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def _authorized(req) -> bool:
    expected = os.environ.get("INGEST_TOKEN", "")
    given = req.headers.get("X-Auth-Token", "")
    return bool(expected) and hmac.compare_digest(given, expected)


# -- pure helpers (unit-tested) -------------------------------------------------
def _round_num(value: Any, digits: int = 1) -> float:
    try:
        return max(0.0, round(float(value), digits))
    except (TypeError, ValueError):
        return 0.0


def _normalize_items(raw: Any) -> List[Dict[str, Any]]:
    """Coerce the model's item list into clean {name, portion_g, +macros} dicts."""
    items: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:120]
        if not name:
            continue
        item = {
            "name": name,
            "portion_g": _round_num(entry.get("portion_g")),
            "calories": _round_num(entry.get("calories")),
            "protein_g": _round_num(entry.get("protein_g")),
            "carbs_g": _round_num(entry.get("carbs_g")),
            "fat_g": _round_num(entry.get("fat_g")),
        }
        method = str(entry.get("cooking_method", "")).strip()[:40]
        if method:
            item["cooking_method"] = method
        items.append(item)
    return items


def _meal_from_items(items: List[Dict[str, Any]], confidence: Any,
                     notes: Any, model: str) -> Dict[str, Any]:
    """Assemble the meal record (row totals = sum over items)."""
    def total(key: str) -> float:
        return round(sum(i[key] for i in items), 1)

    return {
        "items": items,
        "foods": ", ".join(i["name"] for i in items) if items else "not food",
        "portion_g": total("portion_g"),
        "calories": total("calories"),
        "protein_g": total("protein_g"),
        "carbs_g": total("carbs_g"),
        "fat_g": total("fat_g"),
        "confidence": _round_num(confidence, 2),
        "notes": str(notes or "")[:300],
        "model": model,
    }


def _day_totals(meal_rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Sum a day's meal rows, skipping non-meals and zero-content rows."""
    totals = {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    for row in meal_rows:
        if str(row.get("foods") or "").strip().lower() in NON_MEALS:
            continue
        macros = {k: _round_num(row.get(k)) for k in totals}
        if max(macros.values()) <= 0:
            continue
        for k, v in macros.items():
            totals[k] += v
    return {k: round(v, 1) for k, v in totals.items()}


def _parse_score(raw: Any) -> float:
    """Validate a subjective-feel score (1-10, halves allowed)."""
    score = round(float(raw), 1)
    if not 1 <= score <= 10:
        raise ValueError(f"score {score} outside 1-10")
    return score


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


# -- Gemini --------------------------------------------------------------------
def analyze(img: bytes, mime: str) -> Dict[str, Any]:
    """Try each model in order; fall back when one is overloaded/unavailable."""
    last_err: Optional[Exception] = None
    for model in _models():
        try:
            resp = _genai().models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=img, mime_type=mime), PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.1,
                ),
            )
            data = json.loads(resp.text)
            items = _normalize_items(data.get("items"))
            return _meal_from_items(items, data.get("confidence"),
                                    data.get("notes"), model)
        except Exception as err:  # 503 overloaded, 429 rate-limited, parse, ...
            last_err = err
            app.logger.warning("model %s unavailable: %s", model, err)
    raise RuntimeError(f"all models failed ({_models()}); last error: {last_err}")


# -- Drive ---------------------------------------------------------------------
def archive_photo(img: bytes, mime: str, when: datetime) -> str:
    """Upload the photo to the user's Drive folder; return a viewable link."""
    folder = os.environ.get("MEALS_FOLDER_ID", "")
    if not folder:
        return ""
    ext = "png" if "png" in mime else "jpg"
    name = f"meal_{when.strftime('%Y%m%d_%H%M%S')}.{ext}"
    media = MediaIoBaseUpload(io.BytesIO(img), mimetype=mime, resumable=False)
    created = _drive().files().create(
        body={"name": name, "parents": [folder]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    return created.get("webViewLink", "")


# -- Sheets --------------------------------------------------------------------
def _read_tab(tab: str) -> List[List[Any]]:
    return (
        _sheets().spreadsheets().values()
        .get(spreadsheetId=_sid(), range=f"{tab}!A1:Z",
             valueRenderOption="UNFORMATTED_VALUE")
        .execute().get("values", [])
    )


def _rows_as_dicts(values: List[List[Any]]) -> List[Dict[str, Any]]:
    if len(values) < 2:
        return []
    return [dict(zip(values[0], row)) for row in values[1:]]


def _todays_meals(today: str) -> List[Dict[str, Any]]:
    try:
        rows = _rows_as_dicts(_read_tab(MEALS_TAB))
    except Exception:  # tab not created yet
        return []
    return [r for r in rows if str(r.get("datetime", "")).startswith(today)]


def _ensure_meals_tab() -> None:
    meta = _sheets().spreadsheets().get(spreadsheetId=_sid()).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if MEALS_TAB not in titles:
        _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(),
            body={"requests": [{"addSheet": {"properties": {"title": MEALS_TAB}}}]},
        ).execute()
    rng = f"{MEALS_TAB}!A1:{LAST_COL}1"
    current = (
        _sheets().spreadsheets().values()
        .get(spreadsheetId=_sid(), range=rng)
        .execute().get("values", [[]])
    )
    # Self-healing: (re)write the header whenever it doesn't match.
    if not current or current[0] != MEALS_HEADERS:
        _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{MEALS_TAB}!A1",
            valueInputOption="RAW", body={"values": [MEALS_HEADERS]},
        ).execute()


def append_meal(nut: Dict[str, Any], photo_url: str, when: datetime,
                image_sha: str) -> None:
    row = [
        when.isoformat(timespec="seconds"),
        nut["foods"],
        json.dumps(nut["items"], ensure_ascii=False),
        nut["calories"], nut["protein_g"], nut["carbs_g"], nut["fat_g"],
        nut["confidence"], photo_url, nut["portion_g"], nut["notes"],
        image_sha,
    ]
    _ensure_meals_tab()
    _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{MEALS_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _write_daily_cell(day: str, header_name: str, value: Any) -> None:
    """Set one cell on daily_summary's row for `day` (appends the row if new)."""
    values = _read_tab(DAILY_TAB)
    header = values[0] if values else []
    if header_name not in header:
        raise RuntimeError(f"column {header_name!r} missing from {DAILY_TAB}")
    idx = header.index(header_name)
    letters = ""
    n = idx + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    for i, row in enumerate(values[1:], start=2):
        if row and str(row[0]) == day:
            _sheets().spreadsheets().values().update(
                spreadsheetId=_sid(), range=f"{DAILY_TAB}!{letters}{i}",
                valueInputOption="RAW", body={"values": [[value]]},
            ).execute()
            return
    new_row: List[Any] = [""] * len(header)
    new_row[0], new_row[idx] = day, value
    _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{DAILY_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [new_row]},
    ).execute()


# -- HTTP ------------------------------------------------------------------------
def _extract_image():
    """Return (bytes, mime) from either a multipart file or the raw body."""
    if request.files:
        f = next(iter(request.files.values()))
        return f.read(), (f.mimetype or "image/jpeg")
    data = request.get_data()
    mime = request.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return data, mime


@app.get("/")
def health():
    return "ok", 200


@app.post("/ingest")
def ingest():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    img, mime = _extract_image()
    if not img:
        return jsonify({"error": "no image received"}), 400

    when = datetime.now(_tz())
    today = when.date().isoformat()
    image_sha = _sha12(img)

    todays = _todays_meals(today)
    if any(r.get("image_sha") == image_sha for r in todays):
        return jsonify({
            "summary": "Duplicate photo — this meal is already logged today.",
            "duplicate": True,
        }), 200

    try:
        nut = analyze(img, mime)
    except Exception as err:
        # Never lose a meal: archive the photo and leave an auditable stub row.
        app.logger.exception("analysis failed")
        try:
            photo_url = archive_photo(img, mime, when)
        except Exception:
            app.logger.exception("drive upload failed")
            photo_url = ""
        stub = _meal_from_items([], 0, f"analysis failed: {err}", "none")
        stub["foods"] = "analysis failed"
        append_meal(stub, photo_url, when, image_sha)
        return jsonify({
            "error": f"analysis failed: {err}",
            "summary": "Analysis failed — photo archived for later review.",
            "photo_url": photo_url,
        }), 502

    # Archiving must never lose the nutrition data, so failures are non-fatal.
    try:
        photo_url = archive_photo(img, mime, when)
    except Exception:
        app.logger.exception("drive upload failed")
        photo_url = ""

    if not nut["items"]:
        return jsonify({
            "summary": "No food detected — nothing logged (photo archived).",
            "photo_url": photo_url,
            "not_food": True,
        }), 200

    append_meal(nut, photo_url, when, image_sha)

    running = _day_totals(todays)
    for key in running:
        running[key] = round(running[key] + nut[key], 1)
    summary = (
        f"Logged: {nut['foods']} (~{int(nut['portion_g'])} g) — "
        f"~{int(nut['calories'])} kcal "
        f"({int(nut['protein_g'])}P/{int(nut['carbs_g'])}C/{int(nut['fat_g'])}F) · "
        f"Today: {int(running['calories'])} kcal "
        f"({int(running['protein_g'])}P/{int(running['carbs_g'])}C/"
        f"{int(running['fat_g'])}F)"
    )
    return jsonify({"summary": summary, "photo_url": photo_url,
                    "today": running, **nut}), 200


@app.post("/feel")
def feel():
    """Log the day's subjective readiness score into daily_summary."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    clearing = "score" in data and data["score"] is None
    if not clearing:
        raw = data.get("score", request.args.get("score"))
        try:
            score = _parse_score(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "score must be a number from 1 to 10"}), 400

    day = str(data.get("date") or request.args.get("date")
              or datetime.now(_tz()).date().isoformat())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    _write_daily_cell(day, "subjective_feel", "" if clearing else score)
    return jsonify({"date": day,
                    "subjective_feel": None if clearing else score}), 200
