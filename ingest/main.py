"""HTTP ingest service: meal photos, body-composition screenshots, subjective feel.

Everything the phone sends arrives on ONE endpoint and is routed by what the image
actually is, so the user has a single button and never has to decide which kind of
thing they are logging.

POST /ingest (X-Auth-Token) — a **screenshot of the smart-scale app**:
  Gemini's first job on every image is to classify it (see ROUTER_PREFIX). A
  screenshot of the scale app's result screen is transcribed — all ten metrics the
  scale computes from bioimpedance (weight, BMI, body fat, subcutaneous fat,
  visceral fat, body water, muscle mass, bone mass, BMR, metabolic age) — and
  merged into `daily_summary`'s body columns.

  The screen prints the reading's own date/time, so the row is keyed on THAT day,
  not on when the screenshot was sent: weigh at 07:00, send at noon, it still lands
  on the right day, and re-sending an old screenshot rewrites its own historical
  row instead of today's. Sending a fresh reading for a day just replaces it.
  The screenshot does land in the Drive meals folder, unused: classification is
  Gemini's job and Gemini now runs on the worker, so /ingest has to archive every
  image before it can know this one wasn't a meal. The numbers *are* the data; the
  file is ignored.

  This replaced the Google Health API, which only ever exposed weight + body fat
  (Fitbit strips the other eight on the way through) and still needed the phone app
  opened to sync at all. Since opening the app is unavoidable, screenshotting it is
  free — and it yields the full set, immediately, with no scheduled pull.

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
  2. archives every photo to the user's Google Drive (skipped when text-only),
  3. enqueues the analysis and replies 202 in a couple of seconds.
  Everything after that happens on /process:
  4. estimates per-ingredient nutrition with Gemini (structured JSON output),
     reasoning across ALL images together — a nutrition label is authoritative
     for its product and is scaled to the portion on the plate. A `note` is
     authoritative context that overrides the estimate ("only ate half" halves
     portions). Text-only meals reuse the same schema but with capped confidence
     — there is no photo to measure against,
  5. appends a row to the `meals` tab (the raw `note` is stored for provenance;
     `photo_url` holds all archived links).
  Non-food inputs are logged as nothing (photos still archived). If every model
  fails for the whole retry window, the photos are archived and a zeroed
  "analysis failed" row keeps the audit trail — a meal is never silently lost.

Why /ingest analyses nothing (the timeout rule):
  The phone's Shortcut fails the entire log if the HTTP call is slow, and the
  model worth waiting for is precisely the one that's often overloaded. Those two
  facts can't be reconciled on one request, so they aren't: /ingest only archives
  and enqueues, and the Cloud Tasks queue's retry window (8 attempts, 5->120 s
  backoff) becomes a patience budget the worker spends on the best model — see
  _worker_kwargs. The phone is done in seconds; the row lands when it lands,
  typically within a minute, in the worst case ~10.
  The cost is that the reply is an acknowledgement, not the macros: results are
  read back from the sheet / the iOS app, not from the Shortcut's response.

POST /ingest (X-Auth-Token) — **a bowel-movement note.** "fiz cocó", "I just pooped" -> sets
  `daily_summary.bowel_movement` = TRUE for the day.

Auth model:
  * Gemini -> AI Studio key (billing-free project => free tier).
  * Sheets -> the runtime service account (Sheet is shared with it).
  * Drive  -> the *user's* OAuth token (service accounts have no Drive quota).
    This is now the only user token in the system.

Clients and required env are initialised lazily so this module imports cleanly
in tests without credentials.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import io
import json
import os
import random
import re
import socket
import ssl
import time
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import google.auth
from flask import Flask, jsonify, request
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from schema.registry import (
    BLOCK_LABELS, BLOCKS, CAUSAL_LABELS, DAILY_COLUMNS, daily_headers, names_in,
    ocr_ranges,
)

app = Flask(__name__)
# Headroom for a few photos in one meal log while staying under Cloud Run's
# ~32 MiB request cap.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

MEALS_TAB = "meals"
DAILY_TAB = "daily_summary"
TEMPLATES_TAB = "templates"

# One row per meal. `items` is a JSON array breaking the plate into ingredients,
# each with its own portion, macros and a `nutrients` map; the flat columns are
# the row totals the daily job rolls up. `model` records which AI analysed the
# photo (audit); `image_sha` powers de-duplication; `template` records which
# measured template supplied the numbers (blank = estimated from the photo).
# Schema changes (add/remove a column) must be mirrored in src/maintenance.py so
# existing rows are realigned.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note", "template",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "N"

# Meals the user has weighed on a real scale. `items` holds the SAME
# per-ingredient JSON shape as meals, so a template is just a canonical, measured
# items array. Matching a photo to one of these replaces the vision estimate with
# these exact numbers, so a repeat meal gets identical values every time.
TEMPLATES_HEADERS = [
    "name", "description", "items", "portion_g",
    "calories", "protein_g", "carbs_g", "fat_g", "created_at", "updated_at",
]
TEMPLATES_LAST_COL = chr(ord("A") + len(TEMPLATES_HEADERS) - 1)  # "J"
# A template's numbers are measured, not guessed — so a matched meal is confident.
TEMPLATE_CONFIDENCE = 0.95

# Rows excluded from all totals (kept in sync with src/run_daily.py NON_MEALS).
NON_MEALS = {"not food", "analysis failed"}

# The ten metrics the smart scale computes, each with a plausibility band for a
# human body — read straight from the shared schema registry, which both container
# images carry. (These used to be hand-mirrored here and in src/sheets.py, with
# only a test holding them together.)
#
# Reading digits off a phone screen is the one place a model can be confidently,
# silently wrong — a misplaced decimal turns 70.05 kg into 7005 kg and poisons
# every chart and trend downstream. Anything outside its band is a misread, not a
# body, so it is dropped rather than written. Bands are deliberately wide: they
# exist to catch OCR nonsense, not to police what a body may be.
BODY_METRICS: Dict[str, Tuple[float, float]] = ocr_ranges()

# A plain text note ("fiz cocó", "I just pooped") sets this TRUE on the day's
# daily_summary row — the whole feature is one boolean. The user goes at most once
# a day, so yes/no is enough; the note itself is not stored anywhere.
BOWEL_COLUMN = "bowel_movement"


def _col_letter_for(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# Read ranges are derived from the schema with headroom, never hard-coded.
_READ_LAST_COL = _col_letter_for(len(daily_headers()) + 40)

# BEST model first — accuracy over speed, because nothing waits on this any more:
# analysis happens entirely in the background worker (see /ingest), so a slow or
# overloaded model costs patience, never a request timeout.
# gemini-3.5-flash is the model we actually want the numbers from. flash-lite is
# the steady fallback (across the 2026-07-12/13 incidents it was the one that
# never hung); gemini-3-flash-preview is a last resort so an outage on both still
# lands a real row instead of a stub.
# Pro is deliberately NOT here, and paying for it would be a DOWNGRADE — don't
# "upgrade" this chain without re-reading the numbers (verified 2026-07-16):
#   * gemini-3.1-pro-preview has NO free tier at all (structural, not a quota
#     blip): a live call 429s with "check your plan and billing details".
#   * It is also OLDER than gemini-3.5-flash (May 2026) and LOSES to it on
#     multimodal understanding (MMMU-Pro ~+3.1% to Flash) — which is precisely
#     this workload: photos of food and scale screenshots. Pro leads only on
#     text reasoning, which we don't do here.
#   * It costs MORE ($2/$12 per 1M vs $1.50/$9) and is ~2.6x slower.
# Enabling billing to reach it would also end the free tier on that project and
# needs a $10 prepay — and Cloud free-trial/Welcome credits are explicitly barred
# from the Gemini API, so it cannot be paid for with credits.
# The chain is NOT walked top-to-bottom on every try — see _worker_kwargs: the
# fallbacks stay locked until the queue's retry window is nearly spent.
DEFAULT_MODELS = "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3-flash-preview"
# Per-model retries once we're walking the chain (patience spent, get a row).
DEFAULT_RETRIES = 3
# Per-attempt retries while we're still holding out for the first model.
DEFAULT_PATIENT_RETRIES = 4
# In-attempt backoff: min(base**n, cap) + uniform(0, jitter) seconds.
# These numbers are set by our REQUEST RATE, not by how long an outage lasts.
# The free tier allows roughly 10 requests/min *per project* (Google no longer
# publishes the figure — AI Studio's dashboard is authoritative), and Cloud Tasks
# re-runs us every 5-120 s, so in-attempt retries stack across attempts. Measured
# over the full 8-attempt window: base=2/cap=10 peaked at 10 calls in a rolling
# minute — i.e. we would manufacture our own 429s on top of Google's 503s.
# base=3/cap=30 peaks at ~7 while still landing ~30 shots at the best model over
# ~10 min. The LONG waiting is Cloud Tasks' job; ours is to not hammer.
# Jitter follows Google's published guidance. It only ever ADDS delay, so it
# cannot push the measured rate back up. Its value here is modest and worth being
# honest about: we are a single client at maxConcurrentDispatches=1, so there is
# no herd of our own to disperse — it only decorrelates us from every other client
# retrying against the same overloaded model.
DEFAULT_BACKOFF_BASE = 3
DEFAULT_BACKOFF_CAP_S = 30
DEFAULT_BACKOFF_JITTER_S = 2
# How many attempts at the END of the queue's retry window are allowed to fall
# back to a weaker model. Everything before them is best-model-only.
DEFAULT_FALLBACK_LAST_N = 2
# Hard caps so a single request can't hang until Cloud Run's request timeout
# (180 s on health-tracker-ingest):
#  * MAX_OUTPUT_TOKENS bounds generation — without it the model can occasionally
#    run one number to tens of thousands of digits, taking minutes and producing
#    unparseable JSON (the cause of the 504s on 2026-07-12);
#  * TIMEOUT_MS is a per-call network backstop (a hung call is the 2026-07-13
#    504 — 120 s was too long, so 60 s);
#  * DEADLINE_S stops us STARTING another model call so late it would cross the
#    request timeout. It is measured from the start of the request (see
#    _analysis_budget), so the worst case is DEADLINE_S + TIMEOUT_MS + the final
#    sheet write = 105 + 60 + ~5 = ~170 s, inside the 180 s timeout.
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_DEADLINE_S = 105
# MUST NOT exceed the Cloud Tasks queue's max-attempts (`meal-ingest`: 8). The
# worker is what ends the retry loop: on attempt TASKS_MAX_ATTEMPTS it writes an
# "analysis failed" stub and returns 200. Set this HIGHER than the queue allows
# and the queue drops the task first — silently, with no stub and no row.
DEFAULT_TASKS_MAX_ATTEMPTS = 8

# The Cloud Run Job a weigh-in wakes (see _trigger_daily_sync). Override with the
# DAILY_JOB env var.
DEFAULT_DAILY_JOB = "health-tracker-daily"

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

# Prepended to every prompt that carries images. One button on the phone sends
# both meal photos and scale screenshots, so the model's first job is to say which
# it is looking at — a hard fork, decided before any analysis, so the two rubrics
# below never bleed into each other. The distinction is visually trivial (a UI
# screenshot full of numbers vs. food), which is what makes it safe to fold into
# the single call the meal path already makes: no extra latency, no second chance
# to hit a free-tier 503.
ROUTER_PREFIX = """FIRST, CLASSIFY THE IMAGE. Everything else follows from this.

Is it a SCREENSHOT of a body-composition / smart-scale phone app — a list of body
metrics like weight, BMI, body fat, muscle mass, bone mass, BMR, metabolic age?
Or is it FOOD — a meal, a drink, a nutrition label, packaging?

  * A screenshot of body metrics -> set `kind` to "body", follow SECTION B and
    SECTION B ONLY. Return `items` as [] and `confidence` as 0. Do not analyse it
    as food; there is no food in it.
  * Anything else -> set `kind` to "meal", leave `body` empty, and follow
    SECTION A.

================================ SECTION A — MEAL ==============================

"""

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

# Appended to the photo prompt when a note is present, so a photo logged after the
# fact ("this yogurt with my lunch") lands at the right hour instead of the capture
# time. Only fires from the note — a plain photo keeps `meal_time` empty and its
# capture time is used. Mirrors the text-only path's time logic.
MEAL_TIME_SUFFIX = """

MEAL TIME — if the NOTE says WHEN this was eaten (a meal name, or an explicit
time), set `meal_time` to the local 24h "HH:MM" it was eaten: breakfast ~08:00,
brunch ~10:30, lunch ~13:00, afternoon snack ~16:30, dinner ~20:00, late/supper
~22:00, or the explicit time given. The current local time is {now_hhmm} — NEVER
return a later time. If the note says nothing about timing, leave `meal_time`
empty (the photo's capture time is used)."""

# Injected whenever the user has saved templates. A template's weights come from a
# real kitchen scale, so matching one replaces the vision estimate with measured
# numbers — the whole point is that the same meal yields IDENTICAL values every
# day. A wrong match would overwrite measured data with a guess, so the bar for
# matching is deliberately high and the server re-validates the name afterwards.
TEMPLATE_MATCH_SUFFIX = """

KNOWN MEAL TEMPLATES — dishes this user has already weighed on a real scale, so
their ingredient weights and nutrition are MEASURED, not estimated:
{catalogue}

If what you see IS one of these dishes, set `template` to its name copied VERBATIM
and explain the match in `reasoning`. The stored measured values are then used
instead of your estimate, so a repeat meal always gets identical numbers. (Still
fill `items` with your own estimate as a fallback — it is discarded on a match.)
THE NOTE OVERRULES YOUR EYES. If the note says this meal IS one of the templates
(names it, or says "the usual X", "we have a template for this"), that is
AUTHORITATIVE — match it even if the photo is ambiguous or looks a little
different. The user knows what they ate. Only refuse when the NOTE ITSELF says it
differs (an extra/missing ingredient, a different size, "not my usual").

Otherwise, judging from the photo alone, match ONLY when you are confident it is
the same dish with the same components. If anything material differs — a
different bread or protein, an extra or missing ingredient, a clearly different
size — leave `template` EMPTY and estimate normally. A wrong match replaces
measured data with a guess; when in doubt, don't.
If the user ate only part of it, still match and set `template_scale` to the
fraction eaten (e.g. 0.5 for half). Otherwise leave `template_scale` at 1."""

# Always injected: lets the user create a template by simply saying so in the note
# (no extra step in the phone Shortcut). The server only honours this when the note
# genuinely mentions a template, so a stray field can't silently persist one.
TEMPLATE_SAVE_SUFFIX = """

SAVING A TEMPLATE — if the NOTE asks to save/remember this meal as a template
(any phrasing, any language), put the name the user gives it in
`save_template_name`, and fill `items` using the EXACT weights stated in the note
(they weighed them on a scale — those grams are ground truth, never override
them). Otherwise leave `save_template_name` empty."""

# Appended last to every image prompt, as the other half of the ROUTER_PREFIX fork.
#
# This is transcription, not estimation — the exact opposite discipline to SECTION
# A, which spends 200 lines teaching the model to infer, assume and fill gaps. That
# habit is poison here: the numbers are already on the screen and any "helpful"
# inference corrupts a measurement. Hence the blunt, repeated NEVER-guess framing.
#
# The trap this prompt exists to defuse: these apps print a "since <date>" summary
# of CHANGES at the top of the screen — "+ 5.35 kg Peso", "+ 1.7 BMI" — using the
# SAME labels as the real readings, directly above them. Read naively, the user's
# weight becomes 5.35 kg. Hence rule 2. _normalize_body's plausibility bands are
# the backstop if it still slips through.
BODY_SECTION = """

============================ SECTION B — BODY METRICS ==========================
(Only when `kind` is "body". Ignore SECTION A entirely — there is no food here.)

You are transcribing a smart-scale app's result screen. This is OCR, NOT
estimation. Report ONLY numbers you can actually read on screen. NEVER infer,
derive, calculate or guess a value; if a metric is not shown, OMIT it. An omitted
metric is fine. An invented one corrupts the record permanently.

1) FIND THE MEASUREMENT TIMESTAMP. The screen shows the date and time of the
reading, in the user's own language (e.g. "4 de julho de 2026 às 19:03" = 4 July
2026, 19:03). Put it in `body.measured_at` as ISO 8601 local time
"YYYY-MM-DDTHH:MM". Leave it empty ONLY if no date is shown anywhere.

2) IGNORE THE "SINCE <DATE>" COMPARISON BLOCK. These apps show a summary of
CHANGES near the top — numbers with a leading + or -, under a heading like "Desde
6 de agosto de 2023" / "Since ...". Those are DIFFERENCES from an old baseline,
not measurements, and they are labelled exactly like the real ones ("+ 5.35 kg
Peso"). NEVER read a value from that block. Read only the metric list that sits
BELOW the measurement date from step 1. If a value has a +/- sign in front of it,
it is a delta — skip it.

3) TRANSCRIBE EACH METRIC into `body`, copying the digits EXACTLY as displayed
(70.05 stays 70.05 — never round it to 70.1). Labels appear in the user's own
language; map them to these keys:
  weight_kg              weight / peso — kg
  bmi                    BMI / IMC
  body_fat_pct           body fat / gordura corporal — %
  subcutaneous_fat_pct   subcutaneous fat / gordura subcutânea — %
  visceral_fat           visceral fat / gordura visceral — a bare index, no unit
  body_water_pct         body water / água no corpo — %
  muscle_mass_kg         muscle mass / massa muscular — kg
  bone_mass_kg           bone mass / massa óssea — kg
  bmr_kcal               BMR / basal metabolic rate / metabolismo basal — kcal
  metabolic_age          metabolic age / idade metabólica — years
Ignore any qualitative badge or commentary printed beside a value ("Elevado",
"acima da média", "Normal") — transcribe the number only. Values are expected in
the units listed; if one is shown in another unit (lb, st), convert it and say so
in `reasoning`.

In `reasoning`, list every metric you read together with the literal on-screen
text you read it from, so the transcription can be audited afterwards."""

# Prepended to every text-only note, the way ROUTER_PREFIX fronts the image path:
# one Shortcut sends every note, so the model's first job is to say what the note
# IS. Today that's meal-vs-bowel; a body reading can't arrive as text (no screen to
# read), so that fork stays on the image side.
#
# "Bowel" is deliberately narrow: it fires only when the note is *just* reporting a
# trip to the toilet, in any language ("fiz cocó", "I pooped"). A note that
# describes food wins as a meal even if it mentions the bathroom in passing, so a
# real meal log is never swallowed. The multilingual examples matter — the user
# writes in Portuguese.
TEXT_ROUTER_PREFIX = """FIRST, CLASSIFY THIS NOTE — this decides everything below.

Is the note simply the user recording that they had a BOWEL MOVEMENT — that they
went to the toilet to defecate / pooped? This may be in ANY language or phrasing:
"I pooped", "just had a poo", "did a number two", "fiz cocó", "acabei de evacuar",
"já fui à casa de banho fazer cocó", "fui de ventre". The note reports only the
event and describes no food eaten.
  * If YES -> set `kind` to "bowel". Set `items` to [] and `confidence` to 0 and
    STOP — do not treat it as food, there is nothing to estimate.
  * If the note instead describes FOOD OR DRINK the user consumed (even if it
    mentions a bathroom visit in passing) -> set `kind` to "meal" and estimate it
    with the rubric below.

===================== MEAL DESCRIPTION (only when kind is "meal") ================

"""

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

# The scale screenshot's ten metrics plus the reading's own timestamp. Every field
# is optional: the model must omit anything it cannot actually read (see
# BODY_SECTION), and _normalize_body drops whatever is implausible on top of that.
BODY_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    property_ordering=["measured_at", *BODY_METRICS],
    properties={
        # Local "YYYY-MM-DDTHH:MM" read off the screen — this is what decides which
        # day's row the reading lands on.
        "measured_at": types.Schema(type=types.Type.STRING),
        **{k: types.Schema(type=types.Type.NUMBER) for k in BODY_METRICS},
    },
)

RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    property_ordering=["kind", "reasoning", "body", "meal_time", "template",
                       "template_scale", "save_template_name", "items",
                       "confidence"],
    properties={
        # The ROUTER_PREFIX fork: "meal" or "body". Decided first, before any
        # analysis, so the model commits to one rubric. Anything but "body" is
        # treated as a meal — the safe default, and the overwhelmingly common case.
        "kind": types.Schema(type=types.Type.STRING),
        "reasoning": types.Schema(type=types.Type.STRING),
        # Filled only when kind == "body"; empty for every meal.
        "body": BODY_RESPONSE_SCHEMA,
        # Optional "HH:MM" (24h local) inferred from a text note ("breakfast",
        # "lunch", or an explicit time). Empty when unknown / for photo meals.
        "meal_time": types.Schema(type=types.Type.STRING),
        # Name of a KNOWN template this meal is, verbatim (empty = estimate it).
        # The server validates it and swaps in the measured items.
        "template": types.Schema(type=types.Type.STRING),
        # Fraction of the template actually eaten (1 = all of it, 0.5 = half).
        "template_scale": types.Schema(type=types.Type.NUMBER),
        # Set only when the note asks to save this meal as a reusable template.
        "save_template_name": types.Schema(type=types.Type.STRING),
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
    required=["kind", "reasoning", "items", "confidence"],
)


# -- lazy config / clients ----------------------------------------------------
def _models() -> List[str]:
    raw = os.environ.get("GEMINI_MODELS", DEFAULT_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        app.logger.warning("%s is not an int; using %d", name, default)
        return default


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
    # The one user-identity token left in the system: a service account has no
    # Drive storage quota of its own, so meal photos must be uploaded as the user.
    creds = Credentials.from_authorized_user_info(
        json.loads(os.environ["DRIVE_OAUTH_TOKEN"])
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


def _trigger_daily_sync(day: str) -> None:
    """Kick the daily job, because the user has just woken up.

    The weigh-in IS the wake signal. A scale screenshot for TODAY means: the night
    is over and scored, yesterday is over, and the watch has synced (the app had to
    open for the user to screenshot it). No clock knows that — the 07:00 cron this
    replaced fired while the user was still asleep, so sleep always landed a day
    late. Now the row is filled minutes after waking.

    Fire-and-forget and never fatal: the weight is already written, and this runs on
    the Cloud Tasks worker, so raising here would retry the whole task and re-write
    the row. A missed kick just means the 11:00 backstop picks it up.

    Skipped when a run is already in flight: `upsert_daily` is read-modify-write
    against a grid snapshot, so two overlapping executions would both fail to find
    a new date and append it TWICE. Cheap to check, and it also absorbs the real
    case of two screenshots sent back to back.
    """
    project = os.environ.get("GCP_PROJECT")
    job = os.environ.get("DAILY_JOB", DEFAULT_DAILY_JOB)
    location = os.environ.get("DAILY_JOB_LOCATION",
                              os.environ.get("TASKS_LOCATION", "europe-west1"))
    if not project:
        app.logger.warning("no GCP_PROJECT; skipping daily sync trigger")
        return
    base = (f"https://run.googleapis.com/v2/projects/{project}/locations/"
            f"{location}/jobs/{job}")
    try:
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(creds)

        running = session.get(f"{base}/executions?pageSize=10", timeout=20)
        if running.ok:
            for execution in running.json().get("executions", []):
                # No completionTime => still going (or never finished).
                if not execution.get("completionTime"):
                    app.logger.info(
                        "daily sync already running (%s); not starting another",
                        execution.get("name", "").rsplit("/", 1)[-1])
                    return

        resp = session.post(f"{base}:run", json={}, timeout=30)
        if resp.ok:
            app.logger.info("weigh-in for %s woke the daily sync", day)
        else:
            app.logger.warning("daily sync trigger returned %s: %s",
                               resp.status_code, resp.text[:200])
    except Exception:
        app.logger.exception("daily sync trigger failed (backstop will cover it)")


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


# -- body composition (the scale screenshot) -----------------------------------
def _normalize_body(raw: Any) -> Dict[str, float]:
    """Keep the metrics the model actually read, discarding anything that isn't a
    plausible human value.

    This is the load-bearing guard on the body path. OCR fails silently and
    confidently — a dropped decimal reads as 7005 kg, and the "+ 5.35 kg" delta
    printed above the real weight reads as a 5 kg body. Either would sail into the
    sheet and wreck every downstream trend, so a value outside its band in
    BODY_METRICS is thrown away and logged rather than trusted."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for key, (low, high) in BODY_METRICS.items():
        value = raw.get(key)
        # bool is an int subclass — exclude it, a True weight is not 1 kg.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            value = round(float(value), 2)
        except (ValueError, OverflowError):  # runaway number -> drop, never crash
            continue
        if low <= value <= high:
            out[key] = value
        else:
            app.logger.warning(
                "body metric %s=%s outside plausible %s-%s — dropped as a misread",
                key, value, low, high)
    return out


def _resolve_measured_at(raw: Any, now: datetime) -> datetime:
    """The reading's own timestamp, as printed on the app screen.

    This is what makes the screenshot self-dating: the row is keyed on when the
    user actually stepped on the scale, not on when they got round to sending the
    photo. Falls back to `now` when the screen shows no date, and never trusts a
    future timestamp (a clock-skewed screenshot must not create tomorrow's row)."""
    text = str(raw or "").strip()
    if not text:
        return now
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        app.logger.warning("unparseable measured_at %r — using now", text[:40])
        return now
    if parsed.tzinfo is None:  # the screen prints local wall-clock time
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed if parsed <= now else now


def _body_row(body: Dict[str, float], measured: datetime) -> Dict[str, Any]:
    """The daily_summary columns a reading fills, keyed on its own local day.

    `lean_mass_kg` is derived here rather than read: the app doesn't show it, but
    it's the number that actually matters for body recomposition (it's what should
    hold steady while weight falls), so the sheet stores it alongside the rest."""
    row: Dict[str, Any] = {
        "date": measured.date().isoformat(),
        **body,
        "body_measured_at": measured.isoformat(timespec="minutes"),
    }
    weight, fat = body.get("weight_kg"), body.get("body_fat_pct")
    if weight is not None and fat is not None:
        row["lean_mass_kg"] = round(weight * (1 - fat / 100), 2)
    return row


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


def _is_stub(row: Dict[str, Any]) -> bool:
    return str(row.get("foods") or "").strip().lower() in NON_MEALS


def _exact_duplicate(image_sha: str, note: str,
                     todays: List[Dict[str, Any]]) -> bool:
    """True only if THIS exact photo/text AND note is already logged today — a
    genuine double-send. Two cases it deliberately does NOT treat as duplicates:
      * a failed "analysis failed"/"not food" stub (same hash) — so a retry can
        still succeed instead of being blocked by its own earlier failure;
      * the SAME photo re-sent with a CHANGED note — that's a correction to get a
        better estimate; it must re-analyse and replace the row (photo de-dup
        keys on the image, which doesn't include the note). See append_meal."""
    note = str(note or "")
    return any(r.get("image_sha") == image_sha and not _is_stub(r)
               and str(r.get("note") or "") == note for r in todays)


def _meal_row_index(values: List[List[Any]], image_sha: str) -> Optional[int]:
    """1-based sheet row of the existing non-stub meal with this image hash (a
    prior version of the same photo, for upsert/correction), else None."""
    if not values:
        return None
    header = values[0]
    try:
        sha_i, foods_i = header.index("image_sha"), header.index("foods")
    except ValueError:
        return None
    for n, r in enumerate(values[1:], start=2):
        foods = str(r[foods_i] if len(r) > foods_i else "").strip().lower()
        if len(r) > sha_i and str(r[sha_i]) == image_sha and foods not in NON_MEALS:
            return n
    return None


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


# -- Gemini --------------------------------------------------------------------
# Google's own transient set (google.genai._api_client._RETRY_HTTP_STATUS_CODES is
# 408, 429, 500, 502, 503, 504) MINUS 429 — see _retry_same_model for why we
# deliberately part company with the SDK on that one code.
_RETRY_SAME_MODEL_CODES = (408, 500, 502, 503, 504)


def _retry_same_model(err: Exception) -> bool:
    """Is another *immediate* call to the SAME model worth making?

    503/500/502/504/408 mean Google is out of capacity. That's their side, it
    clears on its own, and waiting it out is the entire point of the ladder.

    429 is the opposite and is why we don't just reuse the SDK's retry set: it
    means WE are over quota (free tier ~10 req/min, a few hundred/day, counted per
    PROJECT). Another call cannot succeed and digs the hole deeper, so this model's
    turn ends immediately. That lands correctly on both kinds of attempt: on a
    held-out one it ends the attempt -> 5xx -> Cloud Tasks waits 5-120 s, which is
    exactly the cure for a rolling-window 429; on a chain-walking one it moves to
    the next model, which has its own quota bucket. Either way we stop asking
    instead of hammering — and if it's the daily quota, only Cloud Tasks' clock or
    another model will ever fix it.

    Anything else (400/403/404) never fixes itself -> next model.
    """
    # Prefer the status code: str(err) embeds the error's details JSON, and a
    # quota error's details legitimately contain numbers like 400 — which the old
    # substring sniff read as a permanent 400.
    if isinstance(err, genai_errors.APIError) and isinstance(err.code, int):
        return err.code in _RETRY_SAME_MODEL_CODES
    # Not an APIError (socket timeout, connection reset, SSL): transient, unless it
    # carries a status we recognise in its text.
    return not any(tok in str(err) for tok in (
        "404", "NOT_FOUND", "400", "INVALID_ARGUMENT", "429", "RESOURCE_EXHAUSTED"))


def _backoff_s(attempt: int) -> float:
    """Seconds to wait before the next call to the same model: exponential, capped,
    plus jitter (see DEFAULT_BACKOFF_* for why these numbers are what they are)."""
    base = float(os.environ.get("GEMINI_BACKOFF_BASE", str(DEFAULT_BACKOFF_BASE)))
    cap = float(os.environ.get("GEMINI_BACKOFF_CAP_S", str(DEFAULT_BACKOFF_CAP_S)))
    jitter = float(os.environ.get("GEMINI_BACKOFF_JITTER_S",
                                 str(DEFAULT_BACKOFF_JITTER_S)))
    return min(base ** attempt, cap) + random.uniform(0, jitter)


def _gen_config(timeout_ms: Optional[int] = None) -> "types.GenerateContentConfig":
    if timeout_ms is None:
        timeout_ms = int(os.environ.get("GEMINI_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        temperature=0.1,
        max_output_tokens=int(os.environ.get(
            "GEMINI_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))),
        # attempts=1 pins the SDK to a SINGLE HTTP call: we own retry (_run_models),
        # because only we know the request deadline and that a 429 must not be
        # retried. This is the SDK's current default too, but only implicitly —
        # `retry_options=None` means "never retry", while google's own docs claim
        # the SDK retries 4x by default. If a future version makes that true, its
        # retries would run INSIDE generate_content, where our deadline can't see
        # them: 5 attempts x (60 s timeout + up to 60 s of its own backoff) would
        # sail past Cloud Run's 180 s and 504 with no stub. Pin it.
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _run_models(contents: List[Any], *, models: Optional[List[str]] = None,
                retries: Optional[int] = None, timeout_ms: Optional[int] = None,
                deadline_s: Optional[float] = None,
                allow_body: bool = True, allow_bowel: bool = False) -> Dict[str, Any]:
    """Send `contents` (photos+prompt or a text prompt) through `models` in order
    and assemble the record from the JSON reply.

    Returns one of three records by the model's `kind` verdict: a meal
    (`kind` == "meal"), a body-composition reading (`kind` == "body", images only),
    or a bowel-movement log (`kind` == "bowel", text only). `allow_body` and
    `allow_bowel` gate which non-meal verdicts each path accepts — an image can't
    be a bowel note and a text note can't be a scale screenshot, so a verdict the
    path can't produce is a hallucination and falls back to "meal".

    Callers own the patience policy, not this function: `_worker_kwargs` decides
    which slice of the chain a given Cloud Tasks attempt may use, so on most
    attempts `models` is a single entry and "fall back" means "give up, 5xx, and
    let the queue re-run us" rather than "answer with a weaker model".

    Three failure modes are treated differently:
      * Google-side capacity errors (503 overloaded, 500, 504, timeout) are
        retried on the SAME model with exponential backoff + jitter (_backoff_s);
      * a 429 is NOT (see _retry_same_model) — that one is our own quota, and more
        calls make it worse. We end the model's turn and let the queue's backoff,
        or the next model's separate quota, do the waiting;
      * an unparseable body (the model runs a number to tens of thousands of
        digits, or truncates at max_output_tokens) is deterministic at this
        temperature, so we skip straight to the next model instead of burning
        the retry budget reproducing the same garbage.
    A wall-clock deadline guarantees we return before Cloud Run's request
    timeout rather than letting Cloud Tasks see a 504 (which on the final attempt
    would skip the stub and lose the meal).
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
                if not _retry_same_model(err):
                    app.logger.warning(
                        "model %s: not worth another call now, moving on: %s",
                        model, err)
                    break
                app.logger.warning("model %s attempt %d/%d failed: %s",
                                   model, attempt, retries, err)
                if attempt < retries and time.monotonic() < deadline:
                    time.sleep(_backoff_s(attempt))
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
            # The router fork. Only a verdict the current path can legitimately
            # produce is honoured; anything else is a meal — the common case and the
            # safe default.
            kind = str(data.get("kind") or "").strip().lower()
            if allow_body and kind == "body":
                body = _normalize_body(data.get("body"))
                app.logger.info("%s read a scale screenshot: %d metric(s)",
                                model, len(body))
                return {
                    "kind": "body", "model": model, "body": body,
                    "measured_at": str(
                        (data.get("body") or {}).get("measured_at") or "").strip(),
                }
            if allow_bowel and kind == "bowel":
                app.logger.info("%s classified the note as a bowel-movement log",
                                model)
                return {"kind": "bowel", "model": model}

            items = _normalize_items(data.get("items"))
            meal = _meal_from_items(items, data.get("confidence"), model)
            meal["kind"] = "meal"
            meal["meal_time"] = str(data.get("meal_time") or "").strip()
            meal["template"] = str(data.get("template") or "").strip()
            meal["template_scale"] = data.get("template_scale")
            meal["save_template_name"] = str(
                data.get("save_template_name") or "").strip()
            return meal
    raise RuntimeError(f"all models failed ({models}); last error: {last_err}")


def _templates_block(templates: Optional[List[Dict[str, Any]]]) -> str:
    """The template rules appended to every prompt: how to MATCH a saved dish
    (only when the user has any) and how to SAVE one from the note (always)."""
    block = ""
    if templates:
        block += TEMPLATE_MATCH_SUFFIX.format(
            catalogue=_template_catalogue(templates))
    return block + TEMPLATE_SAVE_SUFFIX


def _build_prompt(num_images: int, note: str, now: Optional[datetime] = None,
                  templates: Optional[List[Dict[str, Any]]] = None) -> str:
    """Assemble the vision prompt as ROUTER + SECTION A (meal) + SECTION B (body).

    Section A is the meal rubric plus its conditional blocks: a multi-image block
    when the log has several photos, the authoritative note block when given, a
    meal-time block (with `now`) so a photo logged after the fact lands at the right
    hour, and the template match/save rules. Section B (transcribing a scale
    screenshot) is constant and always last. The router at the top picks one."""
    prompt = ROUTER_PREFIX + PROMPT
    if num_images > 1:
        prompt += MULTI_IMAGE_SUFFIX.format(n=num_images)
    if note:
        prompt += NOTE_SUFFIX.format(note=note)
        if now is not None:
            prompt += MEAL_TIME_SUFFIX.format(now_hhmm=now.strftime("%H:%M"))
    return prompt + _templates_block(templates) + BODY_SECTION


def analyze(images: List[Tuple[bytes, str]], note: str = "",
            now: Optional[datetime] = None,
            templates: Optional[List[Dict[str, Any]]] = None,
            **kw) -> Dict[str, Any]:
    """Analyse the image(s) the phone sent — either a meal or a scale screenshot;
    the model decides which (see ROUTER_PREFIX) and the returned record's `kind`
    says what came back.

    For a meal, all images are reasoned across together. A `note`, if given, is
    appended as authoritative context that overrides the visual estimate where the
    two conflict; with `now` it can also infer the meal's hour from the note.
    `templates` lets the model recognise a dish the user has weighed and hand back
    its name instead of estimating. `kw` overrides (models/retries/timeout_ms/
    deadline_s) carry the worker's per-attempt patience policy (_worker_kwargs)."""
    parts: List[Any] = [types.Part.from_bytes(data=img, mime_type=mime)
                        for img, mime in images]
    parts.append(_build_prompt(len(images), note, now, templates))
    return _run_models(parts, **kw)


def analyze_text(note: str, now: datetime,
                 templates: Optional[List[Dict[str, Any]]] = None,
                 **kw) -> Dict[str, Any]:
    """Classify a text-only note and act on it: a bowel-movement log
    (`kind` == "bowel", see TEXT_ROUTER_PREFIX), or otherwise a meal estimated from
    the description alone. `now` is the current local time, injected so the model
    can infer a meal's hour and never place it in the future. Templates match here
    too ("o meu pequeno-almoço do costume")."""
    prompt = (TEXT_ROUTER_PREFIX
              + TEXT_PROMPT.format(note=note, now_hhmm=now.strftime("%H:%M"))
              + _templates_block(templates))
    # A text note can be a meal or a bowel log, never a scale reading (no screen to
    # OCR) — so open the bowel fork and close the body one.
    return _run_models([prompt], allow_body=False, allow_bowel=True, **kw)


def _max_attempts() -> int:
    return _int_env("TASKS_MAX_ATTEMPTS", DEFAULT_TASKS_MAX_ATTEMPTS)


def _analysis_budget(started: float) -> float:
    """Seconds left for Gemini, counted from the START of the request rather than
    from the first model call — the sheet reads and Drive downloads that precede
    analysis come out of the same budget, so they can't push the response past
    Cloud Run's request timeout. May go negative (then _run_models gives up at
    once), which is what we want: better a retry than a 504 with no stub."""
    total = float(os.environ.get("GEMINI_DEADLINE_S", str(DEFAULT_DEADLINE_S)))
    return total - (time.monotonic() - started)


def _worker_kwargs(attempt: int) -> Dict[str, Any]:
    """Which models the worker may use on `attempt` (0-based, as Cloud Tasks
    counts it in X-CloudTasks-TaskRetryCount).

    The queue's retry window is a *patience budget*, and the point of this
    function is to spend it on the best model instead of settling early. Walking
    the chain top-to-bottom on every attempt would defeat the ordering entirely:
    the first attempt would drop to flash-lite within seconds of a 3.5-flash
    hiccup and answer with the weaker model, and the remaining seven attempts —
    the patience — would never be used at all.

    So: every attempt but the last few calls ONLY the first model, retrying it
    within the attempt and then handing back a 5xx so Cloud Tasks re-runs us after
    a backoff. With the queue's 8 attempts and 5→120 s backoff that is ~30 shots
    at gemini-3.5-flash spread over ~11 minutes (measured) before anything weaker
    is allowed to answer — at a peak of ~6 calls/min, which is what keeps us clear
    of the free tier's ~10 RPM (see DEFAULT_BACKOFF_BASE).

    On the last FALLBACK_LAST_N attempts patience runs out and we walk the whole
    chain, one shot per model — a model that hangs for the full TIMEOUT_MS must
    not eat the budget the next one needs — because a row from flash-lite beats
    the "analysis failed" stub that the final attempt would otherwise write.
    """
    models = _models()
    fallback_from = _max_attempts() - _int_env(
        "GEMINI_FALLBACK_LAST_N", DEFAULT_FALLBACK_LAST_N)
    if attempt + 1 > fallback_from:
        return {"models": models, "retries": 1}
    return {"models": models[:1],
            "retries": _int_env("GEMINI_PATIENT_RETRIES", DEFAULT_PATIENT_RETRIES)}


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
    # Width is derived from the schema, never hard-coded: `A1:BZ` was exactly 78
    # columns and the registry is already 79, so this read had one column of
    # headroom left. A short range truncates the header silently — the column past
    # the cut looks "missing" and its writes land nowhere.
    return _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=f"{tab}!A1:{_READ_LAST_COL}",
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


def _tab_id(tab: str) -> Optional[int]:
    """The tab's numeric sheetId (needed to sort it), or None if absent."""
    meta = _execute(lambda: _sheets().spreadsheets().get(spreadsheetId=_sid()))
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab:
            return sheet["properties"]["sheetId"]
    return None


def _sort_daily_by_date() -> None:
    """Order daily_summary by date after a new day is appended.

    A screenshot of an *older* reading (the backfill path — scrolling the scale
    app's history) appends a day that belongs above the rows already there. Left
    unsorted it plots out of sequence on the dashboard's trend chart, which is
    worse than useless. Cosmetic only — every roll-up keys on the date column, not
    row order — so the caller swallows failures."""
    tab_id = _tab_id(DAILY_TAB)
    if tab_id is None:
        return
    _execute(lambda: _sheets().spreadsheets().batchUpdate(
        spreadsheetId=_sid(), body={"requests": [{"sortRange": {
            "range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 0},
            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
        }}]}))


# -- templates (measured, reusable meals) --------------------------------------
def _parse_items_cell(raw: Any) -> List[Dict[str, Any]]:
    """The `items` cell holds a JSON array of per-ingredient objects."""
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def read_templates() -> List[Dict[str, Any]]:
    """The user's measured meal templates. Never fatal: a missing/broken tab just
    means no templates, and analysis falls back to estimating."""
    try:
        rows = _rows_as_dicts(_read_tab(TEMPLATES_TAB))
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        items = _normalize_items(_parse_items_cell(row.get("items")))
        if name and items:
            out.append({"name": name,
                        "description": str(row.get("description") or "").strip(),
                        "items": items})
    return out


def _template_catalogue(templates: List[Dict[str, Any]]) -> str:
    """Compact listing injected into the prompt so the model can recognise a
    saved dish: name, what it is, and its measured ingredients."""
    lines = []
    for t in templates:
        parts = ", ".join(f"{i['name']} {int(i['portion_g'])}g" for i in t["items"])
        kcal = int(sum(i["calories"] for i in t["items"]))
        desc = f" — {t['description']}" if t["description"] else ""
        lines.append(f'- "{t["name"]}"{desc} [{parts}] ~{kcal} kcal')
    return "\n".join(lines)


def _forced_template(note: str,
                     templates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A note that says "template" AND spells out a known template's name is an
    explicit instruction, not a hint — honour it deterministically instead of
    leaving recognition to the model's eyes. This is the user's 100%-reliable
    lever when they don't want a repeat meal re-estimated.

    (Save-requests are resolved before this, so "save as template X" can't be
    mistaken for "use template X". The longest matching name wins, so a template
    called "Sandes mista" can't shadow "Sandes mista com ovo".)"""
    text = " ".join(str(note or "").lower().split())
    if "template" not in text:
        return None
    best: Optional[Dict[str, Any]] = None
    for tpl in templates:
        name = " ".join(tpl["name"].lower().split())
        if name and name in text:
            if best is None or len(name) > len(" ".join(best["name"].lower().split())):
                best = tpl
    return best


def _find_template(templates: List[Dict[str, Any]],
                   name: str) -> Optional[Dict[str, Any]]:
    """Look a template up by name, case/space-insensitively. Returns None for a
    name the model invented — the estimate is then kept instead."""
    key = " ".join(str(name or "").lower().split())
    for t in templates:
        if " ".join(t["name"].lower().split()) == key:
            return t
    return None


def _scale_items(items: List[Dict[str, Any]], factor: float) -> List[Dict[str, Any]]:
    """Scale a template's measured items (portion, macros and every nutrient) by
    the fraction actually eaten."""
    if factor == 1:
        return [dict(i) for i in items]
    out: List[Dict[str, Any]] = []
    for item in items:
        scaled = dict(item)
        for key in ("portion_g", "calories", "protein_g", "carbs_g", "fat_g"):
            scaled[key] = _round_num(item.get(key, 0) * factor)
        if item.get("nutrients"):
            scaled["nutrients"] = {
                k: round(v * factor, 2 if k.endswith("_g") else 1)
                for k, v in item["nutrients"].items()
            }
        out.append(scaled)
    return out


def apply_template(nut: Dict[str, Any],
                   templates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Swap the model's *estimate* for a template's *measured* values when it
    recognised a saved dish. An unknown name (a hallucination) is ignored and the
    estimate kept, so a bad match can never invent numbers."""
    name = str(nut.get("template") or "").strip()
    if not name:
        return nut
    tpl = _find_template(templates, name)
    if not tpl:
        app.logger.warning("model returned unknown template %r — keeping estimate",
                           name)
        nut["template"] = ""
        return nut

    scale = _round_num(nut.get("template_scale"), 2)
    if scale <= 0:
        scale = 1.0
    scale = min(scale, 3.0)  # a sane cap; the note drives fractions, not multiples

    meal = _meal_from_items(_scale_items(tpl["items"], scale),
                            TEMPLATE_CONFIDENCE, nut.get("model", ""))
    meal["meal_time"] = nut.get("meal_time", "")
    meal["template"] = tpl["name"] if scale == 1 else f"{tpl['name']} (x{scale:g})"
    app.logger.info("template %r applied (scale %s)", tpl["name"], scale)
    return meal


def _ensure_templates_tab() -> None:
    meta = _execute(lambda: _sheets().spreadsheets().get(spreadsheetId=_sid()))
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if TEMPLATES_TAB not in titles:
        _execute(lambda: _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(),
            body={"requests": [{"addSheet": {
                "properties": {"title": TEMPLATES_TAB}}}]}))
    rng = f"{TEMPLATES_TAB}!A1:{TEMPLATES_LAST_COL}1"
    current = _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=rng)).get("values", [[]])
    if not current or current[0] != TEMPLATES_HEADERS:
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{TEMPLATES_TAB}!A1",
            valueInputOption="RAW", body={"values": [TEMPLATES_HEADERS]}))


def save_template(name: str, nut: Dict[str, Any], when: datetime) -> None:
    """Upsert a template from an analysed meal (its items carry the exact weights
    the user stated in the note). Re-saving the same name updates it in place."""
    _ensure_templates_tab()
    values = _read_tab(TEMPLATES_TAB)
    row = [
        name, nut["foods"], json.dumps(nut["items"], ensure_ascii=False),
        nut["portion_g"], nut["calories"], nut["protein_g"], nut["carbs_g"],
        nut["fat_g"], when.isoformat(timespec="seconds"),
        when.isoformat(timespec="seconds"),
    ]
    idx = None
    if values:
        header = values[0]
        if "name" in header:
            n_i = header.index("name")
            key = " ".join(name.lower().split())
            for i, r in enumerate(values[1:], start=2):
                if len(r) > n_i and " ".join(str(r[n_i]).lower().split()) == key:
                    idx = i
                    break
    if idx is not None:
        row[8] = values[idx - 1][8] if len(values[idx - 1]) > 8 else row[8]  # keep created_at
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(),
            range=f"{TEMPLATES_TAB}!A{idx}:{TEMPLATES_LAST_COL}{idx}",
            valueInputOption="RAW", body={"values": [row]}))
    else:
        _execute(lambda: _sheets().spreadsheets().values().append(
            spreadsheetId=_sid(), range=f"{TEMPLATES_TAB}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]}))


def maybe_save_template(nut: Dict[str, Any], note: str,
                        when: datetime) -> str:
    """Persist this meal as a template when the note asked for it. Guarded twice:
    the model must name it AND the note must actually mention a template, so a
    stray field can never silently create one. Returns the saved name (or "")."""
    name = str(nut.get("save_template_name") or "").strip()
    if not name or "template" not in note.lower():
        return ""
    try:
        save_template(name, nut, when)
    except Exception:
        app.logger.exception("saving template %r failed", name)
        return ""
    app.logger.info("template %r saved", name)
    return name


def append_meal(nut: Dict[str, Any], photo_url: str, when: datetime,
                image_sha: str, note: str = "") -> None:
    row = [
        when.isoformat(timespec="seconds"),
        nut["foods"],
        json.dumps(nut["items"], ensure_ascii=False),
        nut["calories"], nut["protein_g"], nut["carbs_g"], nut["fat_g"],
        nut["confidence"], nut["model"], photo_url, nut["portion_g"],
        image_sha, note, str(nut.get("template") or ""),
    ]
    meals_id = _ensure_meals_tab()
    # Upsert: a photo re-sent with a corrected note replaces its own row rather
    # than duplicating (image_sha is the photo's identity and excludes the note).
    idx = _meal_row_index(_read_tab(MEALS_TAB), image_sha)
    if idx is not None:
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{MEALS_TAB}!A{idx}:{LAST_COL}{idx}",
            valueInputOption="RAW", body={"values": [row]}))
    else:
        _execute(lambda: _sheets().spreadsheets().values().append(
            spreadsheetId=_sid(), range=f"{MEALS_TAB}!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]}))
    if meals_id is not None:
        try:  # the meal is already saved; ordering must never fail the request
            _sort_meals_by_datetime(meals_id)
        except Exception:
            app.logger.warning("meals sort failed (non-fatal)", exc_info=True)


def _col_letter(index: int) -> str:
    """0-based column index -> A1 letter, e.g. 0->'A', 26->'AA'."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def write_daily(day: str, values: Dict[str, Any]) -> None:
    """Merge named columns into daily_summary's row for `day`, appending the row if
    that day is new.

    Only the given columns are touched, which is what lets three independent writers
    share one row: the scale screenshot owns the body columns, and the daily
    job owns the nutrition roll-up. Re-sending a
    reading for a day simply overwrites its own columns again.

    Raises if a column is missing rather than guessing at a position — a stale sheet
    must fail loudly, not shift every value one column to the left."""
    grid = _read_tab(DAILY_TAB)
    header = grid[0] if grid else []
    missing = [name for name in values if name not in header]
    if missing:
        raise RuntimeError(
            f"column(s) {missing} missing from {DAILY_TAB} — run `python -m src.maintenance`")

    for rownum, row in enumerate(grid[1:], start=2):
        if row and str(row[0]) == day:
            data = [{"range": f"{DAILY_TAB}!{_col_letter(header.index(name))}{rownum}",
                     "values": [[value]]}
                    for name, value in values.items()]
            _execute(lambda: _sheets().spreadsheets().values().batchUpdate(
                spreadsheetId=_sid(),
                body={"valueInputOption": "RAW", "data": data}))
            return

    new_row: List[Any] = [""] * len(header)
    new_row[0] = day
    for name, value in values.items():
        new_row[header.index(name)] = value
    _execute(lambda: _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{DAILY_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [new_row]}))
    try:  # the row is already saved; ordering must never fail the request
        _sort_daily_by_date()
    except Exception:
        app.logger.warning("daily_summary sort failed (non-fatal)", exc_info=True)


# -- HTTP ------------------------------------------------------------------------
def _extract_images() -> List[Tuple[bytes, str]]:
    """Every meal image in the request as (bytes, mime): all file parts of a
    multipart upload (any field names, repeats included), or a single raw image
    body. Empty when the request carries no image (the text-only meal path).

    Only a genuine image body counts: a form/JSON request with no file part is
    treated as image-less so its raw bytes are never mistaken for a photo.

    iOS Shortcuts packs several photos into ONE multipart part with the JPEGs
    concatenated (not separate parts), so each part's bytes are split back into
    individual images — otherwise only the first photo of a multi-shot meal is
    ever seen (see 2026-07-13 dinner)."""
    def expand(data: bytes, mime: str) -> List[Tuple[bytes, str]]:
        return [(seg, mime) for seg in _split_jpegs(data)]

    json_images = _images_from_json()
    if json_images:
        return json_images

    if request.files:
        out: List[Tuple[bytes, str]] = []
        for _, f in request.files.items(multi=True):
            data = f.read()
            if data:
                out.extend(expand(data, f.mimetype or "image/jpeg"))
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
    return expand(data, mime)


def _jpeg_end(data: bytes, start: int) -> int:
    """Index just past the EOI (FF D9) of the JPEG that begins at `start`. Walks
    the marker structure, skipping length-delimited segments — so a nested EXIF
    *thumbnail* JPEG (its own FF D8/FF D9 living inside an APPn segment) can't be
    mistaken for an image boundary. Falls back to end-of-data if malformed."""
    n = len(data)
    i = start + 2  # past the SOI (FF D8)
    while i + 1 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1                          # fill byte
        elif marker == 0x00 or 0xD0 <= marker <= 0xD7:
            i += 2                          # stuffed FF, or restart marker (in scan)
        elif marker in (0x01,):
            i += 2                          # standalone marker, no payload
        elif marker == 0xD9:
            return i + 2                    # EOI — end of this image
        elif marker == 0xD8:
            i += 2                          # stray SOI, keep scanning
        elif i + 3 < n:                     # APPn/DQT/DHT/SOF/SOS/... length-delimited
            i += 2 + ((data[i + 2] << 8) | data[i + 3])
        else:
            break
    return n


def _split_jpegs(data: bytes) -> List[bytes]:
    """Split a buffer of one-or-more concatenated JPEGs into individual images.
    A single JPEG (even with an embedded thumbnail) returns unchanged; non-JPEG
    data (e.g. HEIC/PNG) is returned as-is."""
    if not data.startswith(b"\xff\xd8"):
        return [data]
    parts: List[bytes] = []
    i, n = 0, len(data)
    while i < n and data[i:i + 2] == b"\xff\xd8":
        end = _jpeg_end(data, i)
        parts.append(data[i:end])
        nxt = data.find(b"\xff\xd8", end)
        if nxt == -1:
            break
        i = nxt
    return parts if len(parts) > 1 else [data]


def _sniff_mime(data: bytes) -> str:
    """Best-effort image mime from magic bytes; defaults to jpeg."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        return "image/heic"
    return "image/jpeg"


def _images_from_json() -> List[Tuple[bytes, str]]:
    """Decode a JSON `images` array of base64 strings (the reliable multi-photo
    path — Shortcuts' multipart file-list only sends the first item). Empty when
    the body isn't JSON or carries no images."""
    if not request.is_json:
        return []
    raw = (request.get_json(silent=True) or {}).get("images")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[Tuple[bytes, str]] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            continue
        try:
            data = base64.b64decode(entry, validate=False)
        except (binascii.Error, ValueError):
            continue
        for seg in _split_jpegs(data):
            if seg:
                out.append((seg, _sniff_mime(seg)))
    return out


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


def _resolve_templates(nut: Dict[str, Any], note: str, when: datetime,
                       templates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Settle the template question for this meal.

    Saving and matching are mutually exclusive: a note asking to SAVE is defining
    a template (its items are the weights the user stated), so it must not also be
    overwritten by a match. Otherwise, if the note explicitly names a template we
    honour that outright (deterministic — no reliance on the model recognising the
    photo); failing that, we use the model's own match. Either way the measured
    values then replace the estimate."""
    saved = maybe_save_template(nut, note, when)
    if saved:
        nut["template"] = saved  # this meal *is* that dish — record it
        return nut

    forced = _forced_template(note, templates)
    if forced:
        if forced["name"] != nut.get("template"):
            app.logger.info("note names template %r — forcing it (model said %r)",
                            forced["name"], nut.get("template") or "nothing")
        nut["template"] = forced["name"]
    elif "template" in note.lower() and not nut.get("template"):
        # The user mentioned a template but nothing matched — surface it rather
        # than silently falling back to an estimate.
        app.logger.warning(
            "note mentions a template but none matched; estimating instead. "
            "note=%r known=%s", note[:120], [t["name"] for t in templates])

    return apply_template(nut, templates)


def _finalize_body(rec: Dict[str, Any], now: datetime):
    """Write a scale reading into daily_summary.

    Keyed on the reading's own date (from the screen), not on `now` — see
    _resolve_measured_at. Runs on the worker now, so the JSON reply only reaches
    Cloud Tasks, which reads nothing but the status."""
    body = rec.get("body") or {}
    if not body:
        return jsonify({
            "summary": "That looks like a scale screenshot, but no readable "
                       "metrics were found — nothing logged.",
            "kind": "body", "not_read": True,
        }), 200

    measured = _resolve_measured_at(rec.get("measured_at"), now)
    row = _body_row(body, measured)
    day = row.pop("date")
    write_daily(day, row)

    # A scale reading for TODAY means the user just woke up — the one moment when
    # last night's sleep is final and yesterday is closed. Wake the daily sync.
    # A backfilled screenshot (an older reading re-sent) is NOT a wake signal, so
    # it only writes its own row and leaves the sync to the backstop.
    if day == now.date().isoformat():
        _trigger_daily_sync(day)

    def shown(key: str, fmt: str) -> Optional[str]:
        return fmt.format(body[key]) if key in body else None

    highlights = [text for text in (
        shown("weight_kg", "{:g} kg"),
        shown("body_fat_pct", "{:g}% fat"),
        shown("muscle_mass_kg", "{:g} kg muscle"),
        shown("bmi", "BMI {:g}"),
        shown("visceral_fat", "visceral {:g}"),
        shown("bmr_kcal", "BMR {:.0f} kcal"),
    ) if text]
    summary = (
        f"⚖️ {measured.strftime('%-d %b %H:%M')} — " + " · ".join(highlights)
        + f" · {len(body)} metrics saved to {day}"
    )
    app.logger.info("body: %d metric(s) -> %s", len(body), day)
    return jsonify({"summary": summary, "kind": "body", "date": day,
                    "measured_at": row["body_measured_at"], "body": body,
                    "lean_mass_kg": row.get("lean_mass_kg")}), 200


def _finalize_bowel(when: datetime):
    """Flag the day's bowel movement. The whole feature is one boolean, keyed on
    the local day the note was sent — the note text is not stored anywhere.
    Setting the flag again is idempotent, so a re-sent note
    or a Cloud Tasks retry is harmless; only ever TRUE, never FALSE (a blank cell is
    'no'). The user goes at most once a day, so yes/no is the whole model."""
    day = when.date().isoformat()
    write_daily(day, {BOWEL_COLUMN: True})
    app.logger.info("bowel movement logged for %s", day)
    return jsonify({
        "summary": f"🚽 Bowel movement logged for {when.strftime('%-d %b')}.",
        "kind": "bowel", "date": day, "bowel_movement": True,
    }), 200


def _finalize(nut: Dict[str, Any], photo_url: str, when: datetime,
              image_sha: str, note: str, text_only: bool,
              todays: List[Dict[str, Any]]):
    """Tail of a successful analysis: stamp the inferred time, log the row (unless
    it's not food), and build the summary + running day totals.

    The sheet write is the point. The JSON is now read only by Cloud Tasks, which
    cares about nothing but the 2xx — it's kept for replaying /process by hand."""
    # If the note said when the meal was eaten (text-only OR a photo logged after
    # the fact, e.g. "this yogurt with my lunch"), the model returns meal_time and
    # the row lands at that hour today, sorting into place. With no timing note
    # meal_time is empty, so _resolve_meal_time keeps the capture time.
    resolved = _resolve_meal_time(nut.get("meal_time"), when)
    time_inferred = resolved != when
    when = resolved

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
    if text_only:
        prefix = f"Logged for {when.strftime('%H:%M')} (from description): "
    elif time_inferred:
        prefix = f"Logged for {when.strftime('%H:%M')}: "
    else:
        prefix = "Logged: "
    tpl = str(nut.get("template") or "")
    summary = (
        f"{prefix}{nut['foods']} (~{int(nut['portion_g'])} g) — "
        f"~{int(nut['calories'])} kcal "
        f"({int(nut['protein_g'])}P/{int(nut['carbs_g'])}C/{int(nut['fat_g'])}F)"
        + (f" · 📐 {tpl}" if tpl else "")  # measured template, not an estimate
        + f" · Today: {int(running['calories'])} kcal "
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

    # De-dupe before doing any work, so a double-tap doesn't archive and queue
    # twice. Best-effort: if the sheet read fails we queue anyway and let the
    # worker re-check before it writes — a slow duplicate check beats a lost meal.
    try:
        if _exact_duplicate(image_sha, note, _todays_meals(today)):
            return jsonify({
                "summary": ("Duplicate description — already logged today."
                            if text_only
                            else "Duplicate — same photo and note already logged "
                                 "today (change the note to re-estimate)."),
                "duplicate": True,
            }), 200
    except Exception:
        app.logger.exception("duplicate pre-check failed; queueing anyway")

    # Archive now — the sheet needs the links and, since the task body is too
    # small to carry photos, the worker fetches the bytes back from Drive.
    # A scale screenshot gets archived here too: nothing has classified the image
    # yet (only Gemini can), and the worker ignores the photo once it turns out to
    # be a body reading. A stray file in the meals folder is the price of never
    # making the phone wait for the classification.
    archived: List[Dict[str, str]] = []
    if images:
        try:
            archived = archive_photos(images, when)
        except Exception:
            app.logger.exception("drive upload failed")
    photo_url = " ".join(a["url"] for a in archived if a.get("url"))

    # Hand off and acknowledge. NOTHING is analysed on this request: the phone's
    # Shortcut fails the whole log if the response is slow, and the model we want
    # (see DEFAULT_MODELS) is exactly the one that's often overloaded — so the
    # request ends here, in seconds, and the worker gets minutes to be patient.
    try:
        _enqueue_process({
            "text_only": text_only, "note": note,
            "when_iso": when.isoformat(timespec="seconds"),
            "image_sha": image_sha, "today": today,
            "photo_url": photo_url, "refs": archived,
        })
        return jsonify({
            "summary": ("Got it — analysing your note now; it'll be in the sheet "
                        "shortly." if text_only else
                        "Got it — analysing your photo now; it'll be in the sheet "
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
    """Background worker invoked by Cloud Tasks: the whole analysis + row insert.

    This is where every meal is now analysed — /ingest only archives and enqueues.
    Nothing is waiting on the response, so the worker can be slow and stubborn:
    it returns 5xx to make Cloud Tasks retry later (that's the insertion
    guarantee), spends most of its attempts on the best model alone
    (_worker_kwargs), and only on the final attempt writes a stub, so the meal is
    never lost."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    started = time.monotonic()
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
    if _exact_duplicate(image_sha, note, todays):  # idempotent: retry after success
        return jsonify({"status": "already-logged"}), 200

    # Cloud Tasks counts the first attempt as 0.
    attempt = int(request.headers.get("X-CloudTasks-TaskRetryCount", "0"))
    max_attempts = _max_attempts()

    kw = _worker_kwargs(attempt)
    templates = read_templates()
    try:
        images = download_photos(refs) if not text_only else []
        # Budgeted from the top of the request, so the reads above are charged to
        # the same deadline that keeps us inside Cloud Run's 180 s timeout.
        kw["deadline_s"] = _analysis_budget(started)
        nut = (analyze_text(note, when, templates, **kw) if text_only
               else analyze(images, note, now=when, templates=templates, **kw))
    except Exception as err:
        if attempt + 1 >= max_attempts:  # give up: leave an auditable stub
            app.logger.exception("worker exhausted after %d attempts; stub", attempt + 1)
            _log_failure_stub(photo_url, when, image_sha, note)
            return jsonify({"status": "failed-stub"}), 200  # 200 => stop retrying
        app.logger.warning("worker attempt %d/%d failed on %s, will retry: %s",
                           attempt + 1, max_attempts, kw["models"], err)
        return jsonify({"error": str(err)}), 500  # 5xx => Cloud Tasks retries

    # Classification happens here now, so every scale screenshot and bowel note
    # arrives through this path. A screenshot was archived to Drive on the way in
    # as if it were a meal, which is harmless: we ignore the photo and write the
    # numbers.
    if nut.get("kind") == "body":
        return _finalize_body(nut, when)
    if nut.get("kind") == "bowel":
        return _finalize_bowel(when)

    nut = _resolve_templates(nut, note, when, templates)
    return _finalize(nut, photo_url, when, image_sha, note, text_only, todays)



# -- read API (the iOS app) -----------------------------------------------------
# The app never talks to Google. It talks to this service, with the same
# X-Auth-Token the Shortcut uses. That matters for a concrete reason: reading the
# sheet directly would require Google credentials inside the app bundle, and
# anything shipped in an iOS binary is extractable. The service account stays here,
# server-side, and the app holds only a token that can be rotated.
#
# It also means the storage can change later (SQLite, Postgres, whatever) without
# touching a line of Swift — the API is the contract, not the spreadsheet.

def _typed(column, raw: Any) -> Any:
    """Coerce a sheet cell to the type the schema declares.

    UNFORMATTED_VALUE already hands back numbers as numbers, but a blank is `""`
    and a boolean may arrive as the string "TRUE". Clients get a real `null` for
    missing rather than an empty string, so `if value == nil` works in Swift."""
    if raw is None or raw == "":
        return None
    if column.dtype == "boolean":
        return raw is True or str(raw).strip().upper() == "TRUE"
    if column.dtype in ("number", "integer"):
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return int(number) if column.dtype == "integer" else number
    return str(raw)


def _day_document(row: Dict[str, Any], blocks: List[str]) -> Dict[str, Any]:
    """One day as nested JSON, grouped by block.

    Nested rather than flat because the blocks are the natural shape of the domain
    (and of the Swift structs generated from the same registry): a sleep view asks
    for `.sleep`, not for twelve loose keys it has to know the names of.
    """
    out: Dict[str, Any] = {"date": row.get("date")}
    for block in blocks:
        if block in ("key", "meta"):
            continue
        values = {c.name: _typed(c, row.get(c.name))
                  for c in DAILY_COLUMNS if c.block == block}
        # Keep the block even when empty, so the shape is stable for a decoder;
        # an all-null block honestly says "not measured that day".
        out[block] = values
    return out


@app.get("/schema")
def schema():
    """The data dictionary, machine-readable.

    Served next to the data so a client — or an agent — can discover what every
    field means, its unit, and *when it was measured*, without shipping a copy of
    this repo. `measures_when` is the one that stops naive correlation: sleep on a
    date happened the night before it.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "blocks": [{"name": b, "label": BLOCK_LABELS[b]} for b in BLOCKS
                   if b not in ("key", "meta")],
        "columns": [{
            "name": c.name, "block": c.block, "type": c.dtype, "unit": c.unit,
            "source": c.source, "measures_when": CAUSAL_LABELS[c.causal],
            "causal_role": c.role, "direction": c.direction, "tier": c.tier,
            "min": (c.range[0] if c.range else None),
            "max": (c.range[1] if c.range else None),
            "description": c.description,
        } for c in DAILY_COLUMNS],
    }), 200


@app.get("/daily")
def daily():
    """Days from daily_summary as nested JSON.

    `?from=&to=`   inclusive ISO dates (default: the last 30 days)
    `?blocks=`     comma-separated subset, e.g. `sleep,recovery` — so the app
                   fetches only the screen it is drawing
    `?tier=1`      headline metrics only

    A year of this is ~390 KB (~50 KB gzipped), which is why no database is
    involved: the whole history fits in a phone's memory several times over.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    today = datetime.now(_tz()).date()
    start = request.args.get("from") or (today - timedelta(days=30)).isoformat()
    end = request.args.get("to") or today.isoformat()
    for value in (start, end):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return jsonify({"error": "from/to must be YYYY-MM-DD"}), 400

    wanted = [b for b in BLOCKS if b not in ("key", "meta")]
    if request.args.get("blocks"):
        asked = [b.strip() for b in request.args["blocks"].split(",") if b.strip()]
        unknown = [b for b in asked if b not in wanted]
        if unknown:
            return jsonify({"error": f"unknown block(s) {unknown}",
                            "known": wanted}), 400
        wanted = asked

    values = _read_tab(DAILY_TAB)
    rows = _rows_as_dicts(values)
    days = [_day_document(r, wanted) for r in rows
            if start <= str(r.get("date", "")) <= end]
    days.sort(key=lambda d: str(d.get("date")))

    if request.args.get("tier") == "1":
        tier1 = {c.name for c in DAILY_COLUMNS if c.tier == 1}
        for day in days:
            for block in wanted:
                day[block] = {k: v for k, v in day[block].items() if k in tier1}

    return jsonify({"from": start, "to": end, "count": len(days),
                    "blocks": wanted, "days": days}), 200


@app.get("/meals")
def meals():
    """The individual meals of one day (default today), for the app's home screen.

    `?date=` an ISO date (default: today, local tz). Newest last, so the list
    reads top-to-bottom as the day happened. Non-meal stubs ("not food",
    "analysis failed") and empty rows are excluded — exactly the rows the daily
    totals skip (_day_totals) — so the list and the totals always agree.

    Unlike /daily (the rolled-up summary) this is the meal-by-meal breakdown the
    daily job aggregates; a client that wants the day's totals plus its meals gets
    both from this one call.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    day = request.args.get("date") or datetime.now(_tz()).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    rows = _todays_meals(day)  # filters the meals tab by the date prefix
    macro_keys = ("calories", "protein_g", "carbs_g", "fat_g")
    meals_out: List[Dict[str, Any]] = []
    for r in rows:
        if _is_stub(r):
            continue
        macros = {k: _round_num(r.get(k)) for k in macro_keys}
        if max(macros.values()) <= 0:  # empty/zero row — as the totals treat it
            continue
        when = str(r.get("datetime") or "")
        meals_out.append({
            "datetime": when,
            "time": when[11:16],  # "HH:MM" off the ISO string
            "foods": str(r.get("foods") or "").strip(),
            "note": str(r.get("note") or "").strip(),
            "template": str(r.get("template") or "").strip(),
            **macros,
        })
    meals_out.sort(key=lambda m: m["datetime"])

    return jsonify({"date": day, "count": len(meals_out),
                    "totals": _day_totals(rows), "meals": meals_out}), 200
