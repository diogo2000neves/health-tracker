"""HTTP ingest service: meal photos + subjective-feel logging.

POST /ingest (X-Auth-Token) — one or more meal photos, a text description, or
a mix:
  * multipart form: any number of image file parts + an optional `note` text
    part (extra images can be a nutrition label, packaging/brand, or an
    ingredient missing from the first shot),
  * or a raw image body (with an optional `?note=` query param),
  * or a text-only meal: `?note=`, a `note` form field, or JSON {"note": ...}.
  Then:
  1. de-duplicates (photos -> combined image hash; text-only -> note hash) so
     double submissions don't double-log,
  2. estimates per-ingredient nutrition with Gemini (structured JSON output),
     reasoning across ALL images together — a nutrition label is authoritative
     for its product and is scaled to the portion on the plate. A `note` is
     authoritative context that overrides the estimate ("only ate half" halves
     portions). Text-only meals reuse the same schema but with capped confidence
     — there is no photo to measure against,
  3. archives every photo to the user's Google Drive (skipped when text-only),
  4. appends a row to the `meals` tab (the raw `note` is stored for provenance;
     `photo_url` holds all archived links),
  5. replies with the meal summary plus the day's running totals.
  Non-food inputs are logged as nothing (photos still archived). If every model
  fails, the photos are archived and a zeroed "analysis failed" row keeps the
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
import socket
import ssl
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
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
# Headroom for a few photos in one meal log while staying under Cloud Run's
# ~32 MiB request cap.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

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
# flash-lite leads: it's the only lite model and, across the 2026-07-12/13
# incidents, the steady one — the bigger flash models are the ones that ran
# numbers to tens of thousands of digits or hung ~120 s and 504'd. The
# background worker still falls through to the stronger models for accuracy.
DEFAULT_MODELS = "gemini-3.1-flash-lite,gemini-3.5-flash,gemini-3-flash-preview"
DEFAULT_RETRIES = 3
# Hard caps so a single request can't hang until Cloud Run's request timeout:
#  * MAX_OUTPUT_TOKENS bounds generation — without it the model can occasionally
#    run one number to tens of thousands of digits, taking minutes and producing
#    unparseable JSON (the cause of the 504s on 2026-07-12);
#  * TIMEOUT_MS is a per-call network backstop (a hung call is the 2026-07-13
#    504 — 120 s was too long, so 60 s);
#  * DEADLINE_S stops us starting another model attempt so late it would cross
#    the request timeout.
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_DEADLINE_S = 105
# The phone-facing sync attempt is deliberately short: one fast model, then hand
# off to the background queue rather than make the phone wait / risk a 504.
DEFAULT_SYNC_TIMEOUT_MS = 35000
DEFAULT_SYNC_DEADLINE_S = 40
# Keep in sync with the Cloud Tasks queue's max-attempts; on the final attempt the
# worker writes an "analysis failed" stub so a meal is never lost even if Gemini
# is unreachable for the whole retry window.
DEFAULT_TASKS_MAX_ATTEMPTS = 8

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

# Appended to PROMPT when the meal log carries more than one image, so the model
# reasons across all of them instead of analysing only the first. Extra images
# typically add ground truth (a nutrition label) or components the plate shot
# missed; the key risks are mis-matching a label to its food and double-counting.
MULTI_IMAGE_SUFFIX = """

MULTIPLE IMAGES — these {n} images all describe ONE meal; reason across ALL of
them together before you list items. Classify each image as one of:
  - the MEAL/PLATE — what is actually being eaten, and in what portion;
  - a NUTRITION LABEL — authoritative per-100 g / per-serving values for ONE
    product: read its numbers and SCALE them to the portion of that food shown on
    the plate. The label overrides your visual macro AND micronutrient estimate
    for that item;
  - PACKAGING / BRAND — identifies the exact product; use its known profile;
  - an EXTRA INGREDIENT not visible on the plate — add it as its own item.
Match every label/package to the food it belongs to. Do NOT double-count: a food
photographed both on the plate and via its bag is ONE item — composition from the
label, portion from the plate. When images disagree, trust the label for what a
food is made of and the plate for how much of it there is. Note in `reasoning`
which image you used for each decision."""

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

MEAL TIME — set `meal_time` to the local 24h "HH:MM" the meal was eaten TODAY,
inferred from the note. Use an explicit time if the note gives one; otherwise map
the meal name to a typical local hour: breakfast ~08:00, brunch ~10:30, lunch
~13:00, afternoon snack ~16:30, dinner ~20:00, late/supper ~22:00. The current
local time is {now_hhmm} — NEVER return a time later than that (you cannot log a
meal in the future). If the note gives no usable time or meal name, leave
`meal_time` empty and it will default to now.

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
    property_ordering=["reasoning", "meal_time", "items", "confidence"],
    properties={
        "reasoning": types.Schema(type=types.Type.STRING),
        # Optional "HH:MM" (24h local) inferred from a text note ("breakfast",
        # "lunch", or an explicit time). Empty when unknown / for photo meals.
        "meal_time": types.Schema(type=types.Type.STRING),
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


def _enqueue_process(payload: Dict[str, Any]) -> None:
    """Hand a meal to the background worker via Cloud Tasks. The queue retries the
    /process call with backoff for its whole window, so a transient Gemini outage
    can't lose the meal. Raises if the queue isn't configured/reachable, so the
    caller can fall back to a stub (never worse than the old synchronous path).

    Images can't ride in the task (Cloud Tasks bodies are ~small), so the payload
    carries their Drive ids and the worker fetches the bytes back."""
    from google.cloud import tasks_v2  # lazy: keeps tests importable without the lib
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        os.environ["GCP_PROJECT"],
        os.environ.get("TASKS_LOCATION", "europe-west1"),
        os.environ["TASKS_QUEUE"],
    )
    client.create_task(parent=parent, task={"http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": os.environ["PROCESS_URL"],
        "headers": {"Content-Type": "application/json",
                    "X-Auth-Token": os.environ.get("INGEST_TOKEN", "")},
        "body": json.dumps(payload).encode("utf-8"),
    }})


# The Sheets/Drive clients are lru-cached, so their httplib2 keep-alive sockets
# outlive a request. On a scale-to-zero instance the peer closes those sockets
# while it sits idle, and the first Google API call after the gap dies with
# BrokenPipe/ConnectionReset (BrokenPipeError is a ConnectionError subclass).
# _execute rebuilds the cached client and retries so an idle instance self-heals.
_CONN_ERRORS = (ConnectionError, socket.timeout, ssl.SSLError)


def _execute(build):
    """Run `build().execute()` resiliently. `build` must return a *fresh* API
    request each call so a retry picks up a rebuilt client (with a live socket)
    after a stale-connection error."""
    for attempt in range(3):
        try:
            return build().execute()
        except _CONN_ERRORS as err:
            if attempt == 2:
                raise
            app.logger.warning("stale API connection, reconnecting (%d): %s",
                               attempt + 1, err)
            _sheets.cache_clear()
            _drive.cache_clear()
            time.sleep(min(2 ** attempt, 4))


# -- pure helpers (unit-tested) -------------------------------------------------
def _round_num(value: Any, digits: int = 1) -> float:
    # OverflowError guards against a runaway number (hundreds of digits) that
    # parses as an int but overflows float() — it must never 500 the request.
    try:
        return max(0.0, round(float(value), digits))
    except (TypeError, ValueError, OverflowError):
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
            try:
                out[key] = round(float(value), 2 if key.endswith("_g") else 1)
            except OverflowError:  # runaway number -> drop the key, don't crash
                continue
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


def _already_logged(image_sha: str, todays: List[Dict[str, Any]]) -> bool:
    """True only if a SUCCESSFUL meal with this hash is already logged today.

    A failed "analysis failed" / "not food" stub carries the same hash (the note
    or photo is identical on re-send), so counting it would let one failure
    permanently block every retry — the meal could never be logged. Skip stubs so
    a retry re-analyses instead of being silently de-duped away."""
    return any(r.get("image_sha") == image_sha
               and str(r.get("foods") or "").strip().lower() not in NON_MEALS
               for r in todays)


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


def _gen_config(timeout_ms: Optional[int] = None) -> "types.GenerateContentConfig":
    if timeout_ms is None:
        timeout_ms = int(os.environ.get("GEMINI_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.1,
        max_output_tokens=int(os.environ.get(
            "GEMINI_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )


def _run_models(contents: List[Any], *, models: Optional[List[str]] = None,
                retries: Optional[int] = None, timeout_ms: Optional[int] = None,
                deadline_s: Optional[float] = None) -> Dict[str, Any]:
    """Send `contents` (photos+prompt or a text prompt) through the fallback
    chain, strongest model first, and assemble the meal from the JSON reply.

    The `quick` sync path and the background worker share this by overriding the
    chain / retries / per-call timeout / wall-clock deadline: the phone gets a
    fast single-model pass, the worker a thorough one.

    Two failure modes are treated differently:
      * transient API errors (503 overloaded, 429, timeout) are retried on the
        SAME model up to GEMINI_RETRIES times with exponential backoff;
      * an unparseable body (the model runs a number to tens of thousands of
        digits, or truncates at max_output_tokens) is deterministic at this
        temperature, so we skip straight to the next model instead of burning
        the retry budget reproducing the same garbage.
    A wall-clock deadline guarantees we return before Cloud Run's request
    timeout rather than letting the caller (the phone) hit a 504.
    """
    models = models or _models()
    if retries is None:
        retries = int(os.environ.get("GEMINI_RETRIES", str(DEFAULT_RETRIES)))
    retries = max(1, retries)
    if deadline_s is None:
        deadline_s = float(os.environ.get("GEMINI_DEADLINE_S", str(DEFAULT_DEADLINE_S)))
    deadline = time.monotonic() + deadline_s
    last_err: Optional[Exception] = None
    for model in models:
        for attempt in range(1, retries + 1):
            if time.monotonic() > deadline:
                app.logger.warning("analysis deadline reached before %s", model)
                raise RuntimeError(
                    f"analysis deadline exceeded; last error: {last_err}")
            try:
                resp = _genai().models.generate_content(
                    model=model, contents=contents,
                    config=_gen_config(timeout_ms))
            except Exception as err:  # network / API error: 503, 429, timeout, ...
                last_err = err
                if _is_permanent(err):
                    app.logger.warning("model %s unusable, skipping: %s", model, err)
                    break
                app.logger.warning("model %s attempt %d/%d failed: %s",
                                   model, attempt, retries, err)
                if attempt < retries and time.monotonic() < deadline:
                    time.sleep(min(2 ** attempt, 10))
                continue
            # Parse in its own guard: a malformed / truncated body raises
            # JSONDecodeError, and a runaway number raises ValueError (Python's
            # 4300-digit int-parse limit). Both are deterministic -> next model.
            try:
                data = json.loads(resp.text)
            except (json.JSONDecodeError, ValueError, TypeError) as perr:
                last_err = perr
                app.logger.warning(
                    "model %s produced unparseable output, next model: %s",
                    model, perr)
                break
            items = _normalize_items(data.get("items"))
            meal = _meal_from_items(items, data.get("confidence"), model)
            meal["meal_time"] = str(data.get("meal_time") or "").strip()
            return meal
    raise RuntimeError(f"all models failed ({models}); last error: {last_err}")


def _build_prompt(num_images: int, note: str) -> str:
    """Assemble the vision prompt: the base rubric, plus a multi-image block when
    the log has several photos, plus the authoritative note block when given."""
    prompt = PROMPT
    if num_images > 1:
        prompt += MULTI_IMAGE_SUFFIX.format(n=num_images)
    if note:
        prompt += NOTE_SUFFIX.format(note=note)
    return prompt


def analyze(images: List[Tuple[bytes, str]], note: str = "", **kw) -> Dict[str, Any]:
    """Analyse one or more photos of a single meal, reasoning across all of them.

    A `note`, if given, is appended as authoritative context that overrides the
    visual estimate where the two conflict. `kw` overrides (models/retries/
    timeout_ms/deadline_s) let the sync path run a quick single-model pass."""
    parts: List[Any] = [types.Part.from_bytes(data=img, mime_type=mime)
                        for img, mime in images]
    parts.append(_build_prompt(len(images), note))
    return _run_models(parts, **kw)


def analyze_text(note: str, now: datetime, **kw) -> Dict[str, Any]:
    """Estimate a meal from a written description alone (no photo). `now` is the
    current local time, injected so the model can infer the meal's hour and never
    place it in the future."""
    return _run_models(
        [TEXT_PROMPT.format(note=note, now_hhmm=now.strftime("%H:%M"))], **kw)


def _quick_kwargs() -> Dict[str, Any]:
    """Overrides for the phone-facing sync attempt: just the fastest model, one
    shot, a tight per-call timeout and deadline. If it can't produce macros in
    that window the meal is handed to the background worker instead of the phone
    waiting (or hitting a 504)."""
    return {
        "models": _models()[:1],
        "retries": 1,
        "timeout_ms": int(os.environ.get("SYNC_TIMEOUT_MS", str(DEFAULT_SYNC_TIMEOUT_MS))),
        "deadline_s": float(os.environ.get("SYNC_DEADLINE_S", str(DEFAULT_SYNC_DEADLINE_S))),
    }


def _resolve_meal_time(hhmm: Any, now: datetime) -> datetime:
    """Map an inferred "HH:MM" onto today's date in the local tz. Falls back to
    `now` when absent/invalid, and never returns a time in the future (you can't
    log a meal you haven't eaten yet)."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(hhmm or "").strip())
    if not m:
        return now
    candidate = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=0, microsecond=0)
    return candidate if candidate <= now else now


# -- Drive ---------------------------------------------------------------------
def _photo_name(when: datetime, mime: str, index: int, total: int) -> str:
    """Drive filename for one photo of a meal; multi-photo meals get a _N suffix
    so several shots at the same second don't share a name."""
    ext = "png" if "png" in mime else "jpg"
    suffix = f"_{index}" if total > 1 else ""
    return f"meal_{when.strftime('%Y%m%d_%H%M%S')}{suffix}.{ext}"


def archive_photos(images: List[Tuple[bytes, str]],
                   when: datetime) -> List[Dict[str, str]]:
    """Upload every photo of a meal to the user's Drive folder; return, in order
    (meal shot first), a dict per photo: {"id", "url", "mime"}. The `id` lets the
    background worker fetch the bytes back for analysis; `url` goes in the sheet."""
    folder = os.environ.get("MEALS_FOLDER_ID", "")
    if not folder or not images:
        return []
    out: List[Dict[str, str]] = []
    for i, (img, mime) in enumerate(images, start=1):
        name = _photo_name(when, mime, i, len(images))
        # rebuild the media on each retry: an upload stream can't be replayed once
        # partially consumed by a broken connection.
        created = _execute(lambda name=name, img=img, mime=mime: _drive().files().create(
            body={"name": name, "parents": [folder]},
            media_body=MediaIoBaseUpload(io.BytesIO(img), mimetype=mime,
                                         resumable=False),
            fields="id,webViewLink",
        ))
        out.append({"id": created.get("id", ""),
                    "url": created.get("webViewLink", ""), "mime": mime})
    return out


def download_photos(refs: List[Dict[str, str]]) -> List[Tuple[bytes, str]]:
    """Fetch archived photos back from Drive by id (used by the background worker,
    since Cloud Tasks payloads are too small to carry the images themselves)."""
    images: List[Tuple[bytes, str]] = []
    for ref in refs:
        fid = ref.get("id")
        if not fid:
            continue
        data = _execute(lambda fid=fid: _drive().files().get_media(fileId=fid))
        if data:
            images.append((data, ref.get("mime") or "image/jpeg"))
    return images


# -- Sheets --------------------------------------------------------------------
def _read_tab(tab: str) -> List[List[Any]]:
    return _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=f"{tab}!A1:Z",
        valueRenderOption="UNFORMATTED_VALUE")).get("values", [])


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


def _ensure_meals_tab() -> Optional[int]:
    """Make sure the meals tab exists with the right header; return its sheetId
    (needed to sort the tab), or None if it couldn't be determined."""
    meta = _execute(lambda: _sheets().spreadsheets().get(spreadsheetId=_sid()))
    sheets = {s["properties"]["title"]: s["properties"]["sheetId"]
              for s in meta.get("sheets", [])}
    meals_id = sheets.get(MEALS_TAB)
    if meals_id is None:
        reply = _execute(lambda: _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(),
            body={"requests": [{"addSheet": {"properties": {"title": MEALS_TAB}}}]}))
        meals_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
    rng = f"{MEALS_TAB}!A1:{LAST_COL}1"
    current = _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=rng)).get("values", [[]])
    # Self-healing: (re)write the header whenever it doesn't match.
    if not current or current[0] != MEALS_HEADERS:
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{MEALS_TAB}!A1",
            valueInputOption="RAW", body={"values": [MEALS_HEADERS]}))
    return meals_id


def _sort_meals_by_datetime(meals_id: int) -> None:
    """Keep the meals tab in chronological order so a back-dated meal (a note
    logged after later meals) slots into place. Cosmetic — every roll-up sums by
    date — so failures here are swallowed by the caller. ISO datetimes sort
    lexicographically, which is chronological."""
    _execute(lambda: _sheets().spreadsheets().batchUpdate(
        spreadsheetId=_sid(), body={"requests": [{"sortRange": {
            "range": {"sheetId": meals_id, "startRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": len(MEALS_HEADERS)},
            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
        }}]}))


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
    meals_id = _ensure_meals_tab()
    _execute(lambda: _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{MEALS_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]}))
    if meals_id is not None:
        try:  # the meal is already saved; ordering must never fail the request
            _sort_meals_by_datetime(meals_id)
        except Exception:
            app.logger.warning("meals sort failed (non-fatal)", exc_info=True)


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
            _execute(lambda i=i: _sheets().spreadsheets().values().update(
                spreadsheetId=_sid(), range=f"{DAILY_TAB}!{letters}{i}",
                valueInputOption="RAW", body={"values": [[value]]}))
            return
    new_row: List[Any] = [""] * len(header)
    new_row[0], new_row[idx] = day, value
    _execute(lambda: _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{DAILY_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [new_row]}))


# -- HTTP ------------------------------------------------------------------------
def _extract_images() -> List[Tuple[bytes, str]]:
    """Every meal image in the request as (bytes, mime): all file parts of a
    multipart upload (any field names, repeats included), or a single raw image
    body. Empty when the request carries no image (the text-only meal path).

    Only a genuine image body counts: a form/JSON request with no file part is
    treated as image-less so its raw bytes are never mistaken for a photo."""
    if request.files:
        out: List[Tuple[bytes, str]] = []
        for _, f in request.files.items(multi=True):
            data = f.read()
            if data:
                out.append((data, f.mimetype or "image/jpeg"))
        return out
    ctype = (request.content_type or "").lower()
    if (ctype.startswith("multipart/form-data")
            or ctype.startswith("application/x-www-form-urlencoded")
            or ctype.startswith("application/json")):
        return []  # form/JSON with no file => no image
    data = request.get_data()
    if not data:
        return []
    mime = ctype if ctype.startswith("image/") else "image/jpeg"
    return [(data, mime)]


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


def _finalize(nut: Dict[str, Any], photo_url: str, when: datetime,
              image_sha: str, note: str, text_only: bool,
              todays: List[Dict[str, Any]]):
    """Shared tail for a successful analysis (sync path AND background worker):
    stamp the inferred time, log the row (unless it's not food), and build the
    phone-facing summary + running day totals."""
    # A text-only meal carries the hour inferred from the note (e.g. "breakfast"),
    # so the row lands at the right time today and sorts into place. Photo meals
    # keep the capture time (now). Same date either way.
    if text_only:
        when = _resolve_meal_time(nut.get("meal_time"), when)

    if not nut["items"]:
        return jsonify({
            "summary": ("No food in the description — nothing logged."
                        if text_only
                        else "No food detected — nothing logged (photos archived)."),
            "photo_url": photo_url,
            "not_food": True,
        }), 200

    append_meal(nut, photo_url, when, image_sha, note)

    running = _day_totals(todays)
    for key in running:
        running[key] = round(running[key] + nut[key], 1)
    prefix = (f"Logged for {when.strftime('%H:%M')} (from description): "
              if text_only else "Logged: ")
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


def _log_failure_stub(photo_url: str, when: datetime, image_sha: str,
                      note: str) -> None:
    """Auditable placeholder so a meal is never silently lost."""
    stub = _meal_from_items([], 0, "none")
    stub["foods"] = "analysis failed"
    append_meal(stub, photo_url, when, image_sha, note)


@app.post("/ingest")
def ingest():
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    images = _extract_images()
    note = _extract_note()
    if not images and not note:
        return jsonify({"error": "no image or description received"}), 400

    text_only = not images
    when = datetime.now(_tz())
    today = when.date().isoformat()
    # Photo meals de-dupe on the combined image bytes (so a re-send of the same
    # set of shots collapses); text-only meals on the note text.
    image_sha = (_sha12(b"".join(img for img, _ in images)) if images
                 else _sha12(("text:" + note).encode("utf-8")))

    todays = _todays_meals(today)
    if _already_logged(image_sha, todays):
        return jsonify({
            "summary": ("Duplicate description — already logged today."
                        if text_only
                        else "Duplicate photo — this meal is already logged today."),
            "duplicate": True,
        }), 200

    # Quick, best-effort pass for instant macros on the phone. If Gemini isn't
    # fast enough, we don't make the phone wait (or risk a 504) — we archive and
    # hand the meal to the background worker, which retries until it lands.
    try:
        nut = (analyze_text(note, when, **_quick_kwargs()) if text_only
               else analyze(images, note, **_quick_kwargs()))
        quick_ok = True
    except Exception as err:
        app.logger.info("quick analysis missed, deferring to worker: %s", err)
        nut, quick_ok = None, False

    # Archive now — the sheet needs the links and the worker needs the bytes.
    archived: List[Dict[str, str]] = []
    if images:
        try:
            archived = archive_photos(images, when)
        except Exception:
            app.logger.exception("drive upload failed")
    photo_url = " ".join(a["url"] for a in archived if a.get("url"))

    if quick_ok:
        return _finalize(nut, photo_url, when, image_sha, note, text_only, todays)

    # Hand off to the background queue (guaranteed-insert path).
    try:
        _enqueue_process({
            "text_only": text_only, "note": note,
            "when_iso": when.isoformat(timespec="seconds"),
            "image_sha": image_sha, "today": today,
            "photo_url": photo_url, "refs": archived,
        })
        return jsonify({
            "summary": ("Queued — your meal is being analysed and will appear "
                        "shortly."),
            "queued": True, "photo_url": photo_url,
        }), 202
    except Exception:
        # Queue unreachable — fall back to the old behaviour (never lose a meal).
        app.logger.exception("enqueue failed; writing stub")
        _log_failure_stub(photo_url, when, image_sha, note)
        return jsonify({
            "summary": "Couldn't analyse now — logged for later review.",
            "photo_url": photo_url,
        }), 502


@app.post("/process")
def process():
    """Background worker invoked by Cloud Tasks: the thorough analysis + row
    insert. Returns 5xx to make Cloud Tasks retry later (that's the guarantee);
    on the final attempt it writes a stub so the meal is never lost."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    text_only = bool(body.get("text_only"))
    note = str(body.get("note") or "")
    image_sha = str(body.get("image_sha") or "")
    today = str(body.get("today") or "")
    photo_url = str(body.get("photo_url") or "")
    refs = body.get("refs") or []
    try:
        when = datetime.fromisoformat(body["when_iso"])
    except (KeyError, ValueError):
        when = datetime.now(_tz())

    todays = _todays_meals(today)
    if _already_logged(image_sha, todays):  # idempotent: a retry after success
        return jsonify({"status": "already-logged"}), 200

    try:
        images = download_photos(refs) if not text_only else []
        nut = (analyze_text(note, when) if text_only
               else analyze(images, note))
    except Exception as err:
        attempt = int(request.headers.get("X-CloudTasks-TaskRetryCount", "0"))
        max_attempts = int(os.environ.get(
            "TASKS_MAX_ATTEMPTS", str(DEFAULT_TASKS_MAX_ATTEMPTS)))
        if attempt + 1 >= max_attempts:  # give up: leave an auditable stub
            app.logger.exception("worker exhausted after %d attempts; stub", attempt + 1)
            _log_failure_stub(photo_url, when, image_sha, note)
            return jsonify({"status": "failed-stub"}), 200  # 200 => stop retrying
        app.logger.warning("worker attempt %d failed, will retry: %s", attempt + 1, err)
        return jsonify({"error": str(err)}), 500  # 5xx => Cloud Tasks retries

    return _finalize(nut, photo_url, when, image_sha, note, text_only, todays)


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
