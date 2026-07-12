"""HTTP ingest service: meal photos + subjective-feel logging.

POST /ingest (X-Auth-Token) — a meal photo, a text description, or both:
  * multipart form: an optional image file part + an optional `note` text part,
  * or a raw image body (with an optional `?note=` query param),
  * or a text-only meal: `?note=`, a `note` form field, or JSON {"note": ...}.
  Then:
  1. de-duplicates (photo -> image hash; text-only -> note hash) so double
     submissions don't double-log,
  2. estimates per-ingredient nutrition with Gemini (structured JSON output);
     a `note` is authoritative context that overrides the visual/text estimate
     (e.g. "only ate half" halves portions). Text-only meals reuse the same
     schema but with capped confidence — there is no photo to measure against,
  3. archives the photo to the user's Google Drive (skipped when text-only),
  4. appends a row to the `meals` tab (the raw `note` is stored for provenance),
  5. replies with the meal summary plus the day's running totals.
  Non-food inputs are logged as nothing (photos still archived). If every model
  fails, the photo is archived and a zeroed "analysis failed" row keeps the
  audit trail — a meal is never silently lost.

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
import time
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

# One row per meal. `items` is a JSON array breaking the plate into ingredients,
# each with its own portion, macros and a `nutrients` map; the flat columns are
# the row totals the daily job rolls up. `model` records which AI analysed the
# photo (audit); `image_sha` powers de-duplication. Schema changes (add/remove a
# column) must be mirrored in src/maintenance.py so existing rows are realigned.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "M"

# Rows excluded from all totals (kept in sync with src/run_daily.py NON_MEALS).
NON_MEALS = {"not food", "analysis failed"}

# Strongest model first (accuracy over speed — response time isn't critical).
# These are the strongest models available on the FREE tier; Pro models require
# a paid plan (429 on the free key), so they're intentionally not here. If you
# enable billing, prepend "gemini-3.1-pro-preview" via the GEMINI_MODELS env.
# Each model is retried GEMINI_RETRIES times (transient 503s) before falling back.
DEFAULT_MODELS = "gemini-3.5-flash,gemini-3-flash-preview,gemini-3.1-flash-lite"
DEFAULT_RETRIES = 3

# Full per-ingredient micronutrient set, stored in each item's `nutrients` map.
# Grouped by unit (suffix _g/_mg/_ug) so values map cleanly to a future relational
# nutrients table. The Tier-1 subset (src/sheets.py TIER1_NUTRIENTS) also rolls up
# into daily_summary. Keep this in sync with the key list in the prompt below.
NUTRIENTS_G = [
    "fiber_g", "sugar_g", "added_sugar_g", "saturated_fat_g",
    "monounsaturated_fat_g", "polyunsaturated_fat_g", "trans_fat_g",
    "omega3_g", "omega6_g",
]
NUTRIENTS_MG = [
    "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg", "magnesium_mg",
    "zinc_mg", "phosphorus_mg", "copper_mg", "manganese_mg", "chloride_mg",
    "cholesterol_mg", "choline_mg", "vitamin_c_mg", "vitamin_e_mg",
    "vitamin_b1_mg", "vitamin_b2_mg", "vitamin_b3_mg", "vitamin_b5_mg",
    "vitamin_b6_mg",
]
NUTRIENTS_UG = [
    "vitamin_a_ug", "vitamin_d_ug", "vitamin_k_ug", "vitamin_b12_ug",
    "folate_ug", "biotin_ug", "selenium_ug", "iodine_ug",
]
NUTRIENT_KEYS = NUTRIENTS_G + NUTRIENTS_MG + NUTRIENTS_UG

PROMPT = """You are an expert nutritionist and food scientist doing computer-
vision meal analysis. Estimate every ingredient in the photo, its cooked weight
in grams, and its macros as accurately as possible. Being honest about
uncertainty matters more than giving confident round numbers.

Work through steps 1-6 IN ORDER inside the `reasoning` field FIRST, then fill in
`items` and `confidence`. Do not skip the reasoning — thinking through scale and
hidden fats before committing to numbers is what makes them accurate.

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

6) MICRONUTRIENTS PER ITEM (fill each item's `nutrients`).
From the identified food and the grams you estimated, estimate its micronutrients
from that food's known nutritional profile, scaled to the portion. Use EXACTLY
these keys and units:
  grams (g):  fiber_g, sugar_g, added_sugar_g, saturated_fat_g,
    monounsaturated_fat_g, polyunsaturated_fat_g, trans_fat_g, omega3_g, omega6_g
  milligrams (mg):  sodium_mg, potassium_mg, calcium_mg, iron_mg, magnesium_mg,
    zinc_mg, phosphorus_mg, copper_mg, manganese_mg, chloride_mg, cholesterol_mg,
    choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg,
    vitamin_b3_mg, vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug):  vitamin_a_ug, vitamin_d_ug, vitamin_k_ug, vitamin_b12_ug,
    folate_ug, biotin_ug, selenium_ug, iodine_ug
Include every nutrient the food contains in a non-negligible amount; OMIT keys
that are essentially zero or trace for that food (do not pad the object with
zeros). Base values on the cooked weight and method from steps 3-4.

CONFIDENCE — use this EXACT scale (0-1) so the score means the same thing no
matter which model produces it. Report ONE value for the whole meal, set by your
least-certain major item:
  0.90-1.00  clear photo, foods unambiguous, a reliable scale reference present
             or a readable nutrition label.
  0.70-0.89  foods clearly identified; portion estimated from a decent reference.
  0.40-0.69  some ambiguity in identity or portion, or only a weak/partial
             reference to work from.
  0.10-0.39  heavy guesswork: no usable scale reference, or occluded/blurry food.

Rules:
- Never omit an ingredient because it is hard to quantify — estimate it and let
  it lower confidence instead of leaving it out.
- Caloric drinks (juice, soda, milk, beer) are items; water, plain tea and black
  coffee are ignored.
- If the image contains no food or drink, return items: []."""

# Appended to PROMPT when the user attaches a note. The note is AUTHORITATIVE:
# it reflects facts about the meal the photo cannot show (what will actually be
# eaten, how it was cooked, a brand/food the model can't see), so it overrides
# the visual estimate wherever the two conflict.
NOTE_SUFFIX = """

USER NOTE — AUTHORITATIVE. The user added the note below about this meal. Treat
it as ground truth and let it override your visual estimate wherever they
conflict: e.g. "only ate half" => halve the portions of the affected items;
"no oil, air-fried" => drop the absorbed-oil fat; a named food or brand overrides
your identification; a stated weight/count overrides your size estimate. Fold it
into your step 1-6 reasoning; do not treat it as a separate item unless it names
extra food. NOTE: {note}"""

# Text-only path: same schema and per-item rigour, but estimating from a written
# description with NO photo. Confidence is capped low because there is no scale
# reference to measure against — the numbers are informed guesses, not readings.
TEXT_PROMPT = """You are an expert nutritionist estimating a meal from a WRITTEN
DESCRIPTION ALONE — there is no photo. Work through the reasoning FIRST inside
`reasoning`, then fill `items` and `confidence`.

1) PARSE what was eaten from the description: each distinct food/drink, any stated
quantities (weights, counts, "a bowl of", "half a", "a handful"), brands, and
cooking method. Honour every number the user gives — a stated amount overrides
any assumption.

2) FILL THE GAPS with typical values. Where the description omits a portion, use a
realistic single serving for that food and SAY you assumed it (that lowers
confidence). Account for what is usually present but unstated — cooking oil,
butter, dressings, added sugar — exactly as you would for a photo; these are the
largest calorie-error source.

3) IDENTIFY EACH ITEM PRECISELY and split composite meals into separate items
("chicken with rice" = two items). Name items in lowercase singular English.

4) WEIGH EACH ITEM (cooked, as eaten) in grams from the stated or typical serving
and the food's density.

5) COMPUTE MACROS PER ITEM (protein/carbs/fat, then calories); sanity-check each
against ~4*protein + 4*carbs + 9*fat. PER-ITEM numbers only — totals are summed
automatically.

6) MICRONUTRIENTS PER ITEM — fill each item's `nutrients` from the food's known
profile scaled to the grams, using EXACTLY these keys and units:
  grams (g):  fiber_g, sugar_g, added_sugar_g, saturated_fat_g,
    monounsaturated_fat_g, polyunsaturated_fat_g, trans_fat_g, omega3_g, omega6_g
  milligrams (mg):  sodium_mg, potassium_mg, calcium_mg, iron_mg, magnesium_mg,
    zinc_mg, phosphorus_mg, copper_mg, manganese_mg, chloride_mg, cholesterol_mg,
    choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg,
    vitamin_b3_mg, vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug):  vitamin_a_ug, vitamin_d_ug, vitamin_k_ug, vitamin_b12_ug,
    folate_ug, biotin_ug, selenium_ug, iodine_ug
Omit keys that are essentially zero/trace for that food.

CONFIDENCE — CAP AT 0.50 (there is no photo). Use this scale:
  0.35-0.50  the description is specific about foods AND amounts.
  0.20-0.34  foods clear but portions had to be assumed.
  0.10-0.19  vague description with heavy guesswork on identity or amount.

Rules:
- Caloric drinks are items; water, plain tea and black coffee are ignored.
- If the text names no food or drink, return items: [].

MEAL DESCRIPTION: {note}"""

# `reasoning` is generated FIRST (property ordering) so the model works through
# scale, hidden fats and portions before committing to numbers — that ordering
# is what improves accuracy. Meal totals are summed in code (see _meal_from_items),
# never by the model, to avoid arithmetic errors.
_NUTRIENT_PROPS = {k: types.Schema(type=types.Type.NUMBER) for k in NUTRIENT_KEYS}

RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    property_ordering=["reasoning", "items", "confidence"],
    properties={
        "reasoning": types.Schema(type=types.Type.STRING),
        "items": types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                property_ordering=["name", "cooking_method", "portion_g",
                                   "calories", "protein_g", "carbs_g", "fat_g",
                                   "nutrients"],
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    "cooking_method": types.Schema(type=types.Type.STRING),
                    "portion_g": types.Schema(type=types.Type.NUMBER),
                    "calories": types.Schema(type=types.Type.NUMBER),
                    "protein_g": types.Schema(type=types.Type.NUMBER),
                    "carbs_g": types.Schema(type=types.Type.NUMBER),
                    "fat_g": types.Schema(type=types.Type.NUMBER),
                    "nutrients": types.Schema(
                        type=types.Type.OBJECT, properties=_NUTRIENT_PROPS),
                },
                required=["name", "portion_g", "calories",
                          "protein_g", "carbs_g", "fat_g"],
            ),
        ),
        "confidence": types.Schema(type=types.Type.NUMBER),
    },
    required=["reasoning", "items", "confidence"],
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


def _normalize_nutrients(raw: Any) -> Dict[str, float]:
    """Keep known, non-negligible nutrient keys, rounded to a sane precision
    (grams to 2 dp, mg/ug to 1 dp). Unknown keys and zeros/traces are dropped."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for key in NUTRIENT_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = round(float(value), 2 if key.endswith("_g") else 1)
    return out


def _normalize_items(raw: Any) -> List[Dict[str, Any]]:
    """Coerce the model's item list into clean {name, portion_g, macros,
    cooking_method?, nutrients?} dicts."""
    items: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()[:120]
        if not name:
            continue
        item: Dict[str, Any] = {
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
        nutrients = _normalize_nutrients(entry.get("nutrients"))
        if nutrients:
            item["nutrients"] = nutrients
        items.append(item)
    return items


def _meal_from_items(items: List[Dict[str, Any]], confidence: Any,
                     model: str) -> Dict[str, Any]:
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
def _is_permanent(err: Exception) -> bool:
    """A not-found / bad-request error won't fix on retry — skip to the next model
    immediately instead of burning the retry budget on it."""
    msg = str(err)
    return any(tok in msg for tok in ("404", "NOT_FOUND", "400", "INVALID_ARGUMENT"))


def _run_models(contents: List[Any]) -> Dict[str, Any]:
    """Send `contents` (a photo+prompt or a text prompt) through the fallback
    chain, strongest model first, and assemble the meal from the JSON reply.

    Each model is retried up to GEMINI_RETRIES times on transient failures
    (503 overloaded, 429, timeouts) with exponential backoff before falling back
    to the next, weaker model — response time isn't critical, accuracy is.
    """
    models = _models()
    retries = max(1, int(os.environ.get("GEMINI_RETRIES", str(DEFAULT_RETRIES))))
    last_err: Optional[Exception] = None
    for model in models:
        for attempt in range(1, retries + 1):
            try:
                resp = _genai().models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RESPONSE_SCHEMA,
                        temperature=0.1,
                    ),
                )
                data = json.loads(resp.text)
                items = _normalize_items(data.get("items"))
                return _meal_from_items(items, data.get("confidence"), model)
            except Exception as err:  # 503 overloaded, 429, parse, not-found, ...
                last_err = err
                if _is_permanent(err):
                    app.logger.warning("model %s unusable, skipping: %s", model, err)
                    break
                app.logger.warning("model %s attempt %d/%d failed: %s",
                                   model, attempt, retries, err)
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"all models failed ({models}); last error: {last_err}")


def analyze(img: bytes, mime: str, note: str = "") -> Dict[str, Any]:
    """Analyse a meal photo. A `note`, if given, is appended as authoritative
    context that overrides the visual estimate where the two conflict."""
    prompt = PROMPT + (NOTE_SUFFIX.format(note=note) if note else "")
    return _run_models([types.Part.from_bytes(data=img, mime_type=mime), prompt])


def analyze_text(note: str) -> Dict[str, Any]:
    """Estimate a meal from a written description alone (no photo)."""
    return _run_models([TEXT_PROMPT.format(note=note)])


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
                image_sha: str, note: str = "") -> None:
    row = [
        when.isoformat(timespec="seconds"),
        nut["foods"],
        json.dumps(nut["items"], ensure_ascii=False),
        nut["calories"], nut["protein_g"], nut["carbs_g"], nut["fat_g"],
        nut["confidence"], nut["model"], photo_url, nut["portion_g"],
        image_sha, note,
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
    """Return (bytes, mime) from a multipart file part or a raw image body, or
    (b"", "") when the request carries no image (the text-only meal path).

    Only a genuine image body counts: a form/JSON request with no file part is
    treated as image-less so its raw bytes are never mistaken for a photo."""
    if request.files:
        f = next(iter(request.files.values()))
        return f.read(), (f.mimetype or "image/jpeg")
    ctype = (request.content_type or "").lower()
    if (ctype.startswith("multipart/form-data")
            or ctype.startswith("application/x-www-form-urlencoded")
            or ctype.startswith("application/json")):
        return b"", ""  # form/JSON with no file => no image
    data = request.get_data()
    if not data:
        return b"", ""
    mime = ctype if ctype.startswith("image/") else "image/jpeg"
    return data, mime


def _extract_note() -> str:
    """Optional free-text description, from a `note` form field, a `?note=` query
    param, or a JSON {"note": ...} body (in that order). Empty when absent."""
    note = request.form.get("note") or request.args.get("note") or ""
    if not note and request.is_json:
        note = (request.get_json(silent=True) or {}).get("note", "") or ""
    return str(note).strip()[:2000]


@app.get("/")
def health():
    return "ok", 200


@app.post("/ingest")
def ingest():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    img, mime = _extract_image()
    note = _extract_note()
    if not img and not note:
        return jsonify({"error": "no image or description received"}), 400

    text_only = not img
    when = datetime.now(_tz())
    today = when.date().isoformat()
    # Photo meals de-dupe on the image; text-only meals on the note text, so an
    # accidental re-send of either doesn't double-log the same day.
    image_sha = _sha12(img) if img else _sha12(("text:" + note).encode("utf-8"))

    todays = _todays_meals(today)
    if any(r.get("image_sha") == image_sha for r in todays):
        return jsonify({
            "summary": ("Duplicate description — already logged today."
                        if text_only
                        else "Duplicate photo — this meal is already logged today."),
            "duplicate": True,
        }), 200

    def _archive() -> str:
        """Archive the photo if there is one; text-only meals have nothing to store."""
        if not img:
            return ""
        try:
            return archive_photo(img, mime, when)
        except Exception:
            app.logger.exception("drive upload failed")
            return ""

    try:
        nut = analyze_text(note) if text_only else analyze(img, mime, note)
    except Exception as err:
        # Never lose a meal: archive the photo and leave an auditable stub row.
        app.logger.exception("analysis failed")
        photo_url = _archive()
        stub = _meal_from_items([], 0, "none")
        stub["foods"] = "analysis failed"
        append_meal(stub, photo_url, when, image_sha, note)
        return jsonify({
            "error": f"analysis failed: {err}",
            "summary": ("Analysis failed — logged for later review."
                        if text_only
                        else "Analysis failed — photo archived for later review."),
            "photo_url": photo_url,
        }), 502

    # Archiving must never lose the nutrition data, so failures are non-fatal.
    photo_url = _archive()

    if not nut["items"]:
        return jsonify({
            "summary": ("No food in the description — nothing logged."
                        if text_only
                        else "No food detected — nothing logged (photo archived)."),
            "photo_url": photo_url,
            "not_food": True,
        }), 200

    append_meal(nut, photo_url, when, image_sha, note)

    running = _day_totals(todays)
    for key in running:
        running[key] = round(running[key] + nut[key], 1)
    prefix = "Logged (from description): " if text_only else "Logged: "
    summary = (
        f"{prefix}{nut['foods']} (~{int(nut['portion_g'])} g) — "
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
