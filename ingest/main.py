"""HTTP ingest service: receive a meal photo, archive it to the user's Google
Drive, estimate nutrition with Vertex AI Gemini, append a row to the Sheet's
"meals" tab (including a link to the stored photo).

Auth model:
  * Gemini  -> the runtime service account (Vertex AI, ADC).
  * Sheets  -> the runtime service account (Sheet is shared with it).
  * Drive   -> the *user's* OAuth token. A service account has no Drive storage
               quota, so photos must be created by the user to land in their
               own Drive storage.

Callers (the iOS Shortcut) must send header X-Auth-Token == INGEST_TOKEN.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import google.auth
from flask import Flask, request, jsonify
from google import genai
from google.genai import types
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
# Tried in order. The newest Flash model is often overloaded (503) on the free
# tier, so we fall back rather than lose the meal.
MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite"
    ).split(",")
    if m.strip()
]
SPREADSHEET_ID = os.environ["HEALTH_SPREADSHEET_ID"]
INGEST_TOKEN = os.environ["INGEST_TOKEN"]
MEALS_FOLDER_ID = os.environ.get("MEALS_FOLDER_ID", "")
TZ = ZoneInfo(os.environ.get("HEALTH_TZ", "Europe/Lisbon"))
MEALS_TAB = "meals"

# One row per meal. `items` is a JSON array breaking the plate into ingredients,
# each with its own portion + macros; the flat calories/protein/carbs/fat columns
# are the row totals (sum of items) that the daily job rolls up. `date` is dropped
# — it's derivable from `datetime`.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "photo_url",
    "portion_g", "notes",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "K"

PROMPT = """You are a meticulous nutrition analyst examining a photo of a meal.

STEP 1 — IDENTIFY EVERY COMPONENT SEPARATELY.
Break the plate into its distinct foods and list each as its own item. A plate of
"meat with rice" is TWO items (the meat and the rice), not one. Name each item
specifically — never a generic category when a specific one is visible. Actively
distinguish look-alikes using visual cues (size relative to other objects,
skin/peel texture, colour, segment count, cut, marbling, packaging): e.g.
tangerine/clementine vs orange; sweet potato vs potato; salmon vs trout;
prosciutto vs bacon vs cooked ham; yoghurt vs cream; white vs brown rice.

STEP 2 — ESTIMATE EACH PORTION.
For every item, find a scale reference (plate/bowl diameter, cutlery, a hand,
a can/bottle, standard packaging) and estimate the real edible weight in grams of
THAT item as actually shown. Do NOT assume standard servings and do NOT default
to 100 g. Exclude inedible parts (peel, rind, bones, shells, stones).

STEP 3 — COMPUTE NUTRITION PER ITEM.
For each item, derive calories and macros for the grams you estimated — not per
100 g. Account for visible cooking fat, oil, sauces, dressings and skin. For each
item, sanity-check: calories must be within ~10% of
(protein_g x 4) + (carbs_g x 4) + (fat_g x 9). Fix the numbers if they disagree.

RULES.
- One food = one item. Split composite plates into their components.
- If an item's identity is uncertain, pick the most likely and LOWER confidence.
- confidence (0-1) reflects identification AND portion certainty across the meal.
- If the image contains NO food, return "items": [].

Reply with ONLY a JSON object, no markdown:
{"items": [{"name": string, "portion_g": number, "calories": number,
 "protein_g": number, "carbs_g": number, "fat_g": number}, ...],
 "confidence": number, "notes": string}
"notes" = one short sentence naming the scale reference you used and your key
assumption for the meal."""

# Sheets: service-account ADC. Gemini: client's own ADC.
_sheets_creds, _ = google.auth.default(
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
_sheets = build("sheets", "v4", credentials=_sheets_creds, cache_discovery=False)
# Gemini Developer API (AI Studio key on a billing-free project => free tier).
_genai = genai.Client(api_key=GEMINI_API_KEY)

# Drive: the user's OAuth token (so files are owned by them, using their quota).
_user_creds = Credentials.from_authorized_user_info(
    json.loads(os.environ["HEALTH_OAUTH_TOKEN"])
)
if not _user_creds.valid:
    _user_creds.refresh(AuthRequest())
_drive = build("drive", "v3", credentials=_user_creds, cache_discovery=False)


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


def _normalize_items(raw) -> list:
    """Coerce the model's item list into clean {name, portion_g, +macros} dicts."""
    def f(x):
        try:
            return round(float(x), 1)
        except (TypeError, ValueError):
            return 0.0

    items = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name", "")).strip()[:120]
        if not name:
            continue
        items.append({
            "name": name,
            "portion_g": f(it.get("portion_g")),
            "calories": f(it.get("calories")),
            "protein_g": f(it.get("protein_g")),
            "carbs_g": f(it.get("carbs_g")),
            "fat_g": f(it.get("fat_g")),
        })
    return items


def analyze(img: bytes, mime: str) -> dict:
    """Try each model in order; fall back when one is overloaded/unavailable."""
    last_err = None
    for model in MODELS:
        try:
            resp = _genai.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=img, mime_type=mime), PROMPT],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.1
                ),
            )
            data = json.loads(resp.text)
            items = _normalize_items(data.get("items"))
            total = lambda k: round(sum(i[k] for i in items), 1)
            return {
                "items": items,
                # Human-readable summary label built from the item names.
                "foods": ", ".join(i["name"] for i in items) if items else "not food",
                "portion_g": total("portion_g"),
                "calories": total("calories"),
                "protein_g": total("protein_g"),
                "carbs_g": total("carbs_g"),
                "fat_g": total("fat_g"),
                "confidence": round(float(data.get("confidence", 0)), 2),
                "notes": str(data.get("notes", ""))[:300],
                "model": model,
            }
        except Exception as e:  # 503 overloaded, 429 rate-limited, 404, ...
            last_err = e
            app.logger.warning("model %s unavailable: %s", model, e)
    raise RuntimeError(f"all models failed ({MODELS}); last error: {last_err}")


def archive_photo(img: bytes, mime: str, when: datetime) -> str:
    """Upload the photo to the user's Drive folder; return a viewable link."""
    if not MEALS_FOLDER_ID:
        return ""
    ext = "png" if "png" in mime else "jpg"
    name = f"meal_{when.strftime('%Y%m%d_%H%M%S')}.{ext}"
    media = MediaIoBaseUpload(io.BytesIO(img), mimetype=mime, resumable=False)
    created = _drive.files().create(
        body={"name": name, "parents": [MEALS_FOLDER_ID]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()
    return created.get("webViewLink", "")


def _ensure_meals_tab() -> None:
    meta = _sheets.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if MEALS_TAB not in titles:
        _sheets.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": MEALS_TAB}}}]},
        ).execute()

    rng = f"{MEALS_TAB}!A1:{LAST_COL}1"
    current = (
        _sheets.spreadsheets().values()
        .get(spreadsheetId=SPREADSHEET_ID, range=rng)
        .execute().get("values", [[]])
    )
    # Self-healing: (re)write the header whenever it doesn't match.
    if not current or current[0] != MEALS_HEADERS:
        _sheets.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range=f"{MEALS_TAB}!A1",
            valueInputOption="RAW", body={"values": [MEALS_HEADERS]},
        ).execute()


def append_meal(nut: dict, photo_url: str, when: datetime) -> None:
    row = [
        when.isoformat(timespec="seconds"),
        nut["foods"],
        json.dumps(nut["items"], ensure_ascii=False),
        nut["calories"], nut["protein_g"], nut["carbs_g"], nut["fat_g"],
        nut["confidence"], photo_url, nut["portion_g"], nut["notes"],
    ]
    _ensure_meals_tab()
    _sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"{MEALS_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


@app.get("/")
def health():
    return "ok", 200


@app.post("/ingest")
def ingest():
    if request.headers.get("X-Auth-Token") != INGEST_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    img, mime = _extract_image()
    if not img:
        return jsonify({"error": "no image received"}), 400

    when = datetime.now(TZ)

    try:
        nut = analyze(img, mime)
    except Exception as e:
        app.logger.exception("analysis failed")
        return jsonify({"error": f"analysis failed: {e}"}), 502

    # Archiving must never lose the nutrition data, so failures are non-fatal.
    try:
        photo_url = archive_photo(img, mime, when)
    except Exception:
        app.logger.exception("drive upload failed")
        photo_url = ""

    append_meal(nut, photo_url, when)
    summary = (
        f"Logged: {nut['foods']} (~{int(nut['portion_g'])} g) — "
        f"~{int(nut['calories'])} kcal "
        f"({int(nut['protein_g'])}P/{int(nut['carbs_g'])}C/{int(nut['fat_g'])}F)"
    )
    return jsonify({"summary": summary, "photo_url": photo_url, **nut}), 200
