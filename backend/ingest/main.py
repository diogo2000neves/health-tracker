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
import subprocess
import time
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import google.auth
from flask import Flask, Response, abort, jsonify, request
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from schema import capabilities as caps_mod
from schema.registry import (
    BLOCK_LABELS, BLOCKS, CAUSAL_LABELS, DAILY_COLUMNS, daily_headers, names_in,
    ocr_ranges,
)
# Flat import, like the coach modules in the image. Safe at module scope because it
# is pure stdlib and touches nothing at import time — importing main.py must stay
# possible with no env and no credentials (test_ingest.py asserts it).
import claude_estimator

app = Flask(__name__)
# Headroom for a few photos in one meal log while staying under Cloud Run's
# ~32 MiB request cap.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

MEALS_TAB = "meals"
DAILY_TAB = "daily_summary"
TEMPLATES_TAB = "templates"
TARGETS_TAB = "targets"
# What this user measures, what they are aiming at, and the body they declared.
# One row per setting; see schema/capabilities.py.
CONFIG_TAB = "config"

# One row per meal. `items` is a JSON array breaking the plate into ingredients,
# each with its own portion, macros and a `nutrients` map; the flat columns are
# the row totals the daily job rolls up. `model` records which AI analysed the
# photo (audit); `image_sha` powers de-duplication; `template` records which
# measured template supplied the numbers (blank = estimated from the photo).
# `edited_at` is set the moment a user hand-corrects an item via /meals/edit — it
# marks the row so the local audit job (automation/nutrition-audit/audit.py) skips
# it instead of clobbering the correction with a fresh photo re-estimate.
# Schema changes (add/remove a column) must be mirrored in src/maintenance.py and
# in automation/nutrition-audit/audit.py's own MEALS_HEADERS so existing rows are
# realigned and the audit job keeps writing the same shape back.
MEALS_HEADERS = [
    "datetime", "foods", "items", "calories",
    "protein_g", "carbs_g", "fat_g", "confidence", "model", "photo_url",
    "portion_g", "image_sha", "note", "template", "edited_at",
]
LAST_COL = chr(ord("A") + len(MEALS_HEADERS) - 1)  # "O"

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
# gemini-3.6-flash is the model we actually want the numbers from (released
# 2026-07-21, successor to 3.5-flash: cheaper output and ~17% fewer output
# tokens for the same task). gemini-3.5-flash-lite is the steady fallback
# (successor to 3.1-flash-lite, same role); gemini-3.5-flash is the last
# resort — the previous generation's primary, kept alive and distinct from the
# top two so an outage on both still lands a real row instead of a stub.
# (gemini-3-flash-preview, the old last resort, was retired by Google on
# 2026-06-25 — it no longer answers at all, which is why this chain moved up
# a generation on 2026-07-21 rather than just swapping the dead entry.)
# Pro is deliberately NOT here, and paying for it would be a DOWNGRADE — don't
# "upgrade" this chain without re-reading the numbers (verified 2026-07-16):
#   * gemini-3.1-pro-preview has NO free tier at all (structural, not a quota
#     blip): a live call 429s with "check your plan and billing details".
#   * It is also OLDER than the Flash line and LOSES to it on multimodal
#     understanding (MMMU-Pro) — which is precisely this workload: photos of
#     food and scale screenshots. Pro leads only on text reasoning, which we
#     don't do here.
#   * It costs MORE per 1M tokens and is meaningfully slower.
# Enabling billing to reach it would also end the free tier on that project and
# needs a $10 prepay — and Cloud free-trial/Welcome credits are explicitly barred
# from the Gemini API, so it cannot be paid for with credits.
# The chain is NOT walked top-to-bottom on every try — see _worker_kwargs: the
# fallbacks stay locked until the queue's retry window is nearly spent.
DEFAULT_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.5-flash"
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
#
# ⚠️ Four nutrients were deliberately REMOVED from this set (2026-07-31) and must
# not be re-added: `vitamin_d_ug`, `vitamin_k_ug`, `biotin_ug`, `chloride_mg`.
# This app has exactly one measurement vector — the food on the plate — and for
# these four the plate is not where the nutrient mostly comes from, so a food-only
# figure isn't a low reading, it's a wrong one:
#   * vitamin D  — cutaneous synthesis from UVB is the dominant source; a single
#                  sun exposure outweighs any plausible day of eating.
#   * vitamin K  — menaquinones (K2) from colonic bacteria, absorbed in the colon.
#   * biotin     — colonic bacteria + the SMVT transporter; a true dietary
#                  requirement has never been established (frank deficiency
#                  essentially requires raw egg white's avidin).
#   * chloride   — co-supplied with every milligram of sodium; intake is
#                  universally sufficient, so a "deficit" here can never be real.
# Measuring these would mean showing a red gauge for a vector we cannot see. The
# nutrition audit's own FDC grounding could not fill chloride or biotin either
# (see automation/nutrition-audit/fdc.py) — the data was never there to begin with.
NUTRIENTS_G = [
    "fiber_g", "sugar_g", "added_sugar_g", "saturated_fat_g",
    "monounsaturated_fat_g", "polyunsaturated_fat_g", "trans_fat_g",
    "omega3_g", "omega6_g",
]
NUTRIENTS_MG = [
    "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg", "magnesium_mg",
    "zinc_mg", "phosphorus_mg", "copper_mg", "manganese_mg",
    "cholesterol_mg", "choline_mg", "vitamin_c_mg", "vitamin_e_mg",
    "vitamin_b1_mg", "vitamin_b2_mg", "vitamin_b3_mg", "vitamin_b5_mg",
    "vitamin_b6_mg",
]
NUTRIENTS_UG = [
    "vitamin_a_ug", "vitamin_b12_ug",
    "folate_ug", "selenium_ug", "iodine_ug",
]
NUTRIENT_KEYS = NUTRIENTS_G + NUTRIENTS_MG + NUTRIENTS_UG

# How many completed days /today returns as the rolling `history` window. Buffered
# ("rolling") nutrients are read against this window's average, so a single low day is
# judged against reserves, not shown as a deficit. Seven smooths daily variance while
# still being short enough to have data for a newer user; it is a tunable constant.
NUTRIENT_HISTORY_DAYS = 7

# -- targets: the per-metric goals every number is shown against --------------
# A number without a target is trivia. The `targets` tab is the source of truth,
# user-visible and editable in the sheet (like `meals`/`templates`, it is created
# and seeded on demand). One row per metric: metric, kind, floor, ceiling, unit,
# source.
#
#   kind    reach  = hit a floor (protein, fibre, every vitamin/most minerals):
#                    under is amber, met is green.
#           limit  = stay under a ceiling (sodium, added sugar, sat/trans fat):
#                    under is green, over is red.
#           window = stay near a value with a floor and a ceiling (calories, the
#                    fill macro carbs).
#   source  measured = computed by the backend from the user's own data
#                      (calories/protein/… — recomputed live on every /today read,
#                      see _derive_targets), so a new weigh-in or a shifting TDEE
#                      moves them without a redeploy;
#           rda      = a static reference default (the micronutrient table below),
#                      seeded once and then owned by the user — editing the cell in
#                      the sheet is respected and never overwritten;
#           manual   = a user override of any metric; it always wins, even over a
#                      `measured` row the backend would otherwise recompute.
TARGETS_TAB_HEADERS = ["metric", "kind", "floor", "ceiling", "unit", "source"]
TARGETS_LAST_COL = chr(ord("A") + len(TARGETS_TAB_HEADERS) - 1)  # "F"

TARGET_REACH, TARGET_LIMIT, TARGET_WINDOW = "reach", "limit", "window"
SRC_MEASURED, SRC_RDA, SRC_MANUAL = "measured", "rda", "manual"
# A number derived from what the user TOLD us (the `config` tab) rather than from
# anything measured — the only layer available to someone with no scale and no
# tracker. Ranks below `measured` and above the built-in constants, and is labelled
# separately so the app never calls a declared number a measured one.
SRC_DECLARED, SRC_DEFAULT = "declared", "default"

# A nutrient's kinetics `horizon` — how a deficit should be read (see _with_kinetics
# and _NUTRIENT_KINETICS). This is intrinsic biology, not a per-user setting, so it is
# stamped onto targets at read time and never stored in the sheet.
#   daily   = not stored in the body in any useful amount; judge TODAY vs the floor —
#             the excess is excreted, so what matters is day-to-day CONSISTENCY.
#   rolling = buffered by body stores (liver, fat, bone) for days to months; judge the
#             rolling AVERAGE — a single low day is covered by reserves, not a deficit.
HORIZON_DAILY, HORIZON_ROLLING = "daily", "rolling"

# Micronutrient reference values for an ADULT MALE, 19-50 (the user: male, 25).
# reach floors are the U.S. DRI RDA where one exists, else the AI; limit ceilings
# are the tolerable/chronic-disease-risk guidance. These are the standard, most
# complete set of references and are seeded as `source=rda` — edit any cell in the
# sheet to personalise it. `added_sugar_g`/`saturated_fat_g` are NOT here: they
# scale with the calorie target, so they are derived in _derive_targets instead.
# `cholesterol_mg` is NOT here either: the fixed 300 mg/day cap was dropped by the
# 2015-2020 Dietary Guidelines for Americans once the evidence showed dietary
# cholesterol is a weak, individually-variable predictor of serum LDL-C for most
# people (serum LDL is set mainly by hepatic LDL-receptor activity, which responds
# far more to saturated fat than to cholesterol intake) — so it's tracked and shown
# (NUTRIENT_KEYS, NutrientCatalog's context section) but carries no target/ceiling.
# Vitamin D, vitamin K, biotin and chloride are absent for a different reason: they
# are not measured at all any more (see NUTRIENT_KEYS) because food is not their
# main vector. Don't re-add a floor for them.
# Values verified 2026-07 against the U.S. National Academies DRI tables and the
# 2025-2030 Dietary Guidelines for Americans.
# Each entry: key -> (kind, floor, ceiling, unit).
_MICRO_TARGETS: Dict[str, Tuple[str, Optional[float], Optional[float], str]] = {
    # fat-soluble vitamins
    "vitamin_a_ug":   (TARGET_REACH, 900,  None, "ug"),   # RDA (µg RAE)
    "vitamin_e_mg":   (TARGET_REACH, 15,   None, "mg"),   # RDA (mg α-tocopherol)
    # water-soluble vitamins
    "vitamin_c_mg":   (TARGET_REACH, 90,   None, "mg"),   # RDA
    "vitamin_b1_mg":  (TARGET_REACH, 1.2,  None, "mg"),   # thiamin RDA
    "vitamin_b2_mg":  (TARGET_REACH, 1.3,  None, "mg"),   # riboflavin RDA
    "vitamin_b3_mg":  (TARGET_REACH, 16,   None, "mg"),   # niacin RDA (mg NE)
    "vitamin_b5_mg":  (TARGET_REACH, 5,    None, "mg"),   # pantothenic acid AI
    "vitamin_b6_mg":  (TARGET_REACH, 1.3,  None, "mg"),   # RDA
    "vitamin_b12_ug": (TARGET_REACH, 2.4,  None, "ug"),   # RDA
    "folate_ug":      (TARGET_REACH, 400,  None, "ug"),   # RDA (µg DFE)
    "choline_mg":     (TARGET_REACH, 550,  None, "mg"),   # AI (male)
    # minerals
    "calcium_mg":     (TARGET_REACH, 1000, None, "mg"),   # RDA
    "iron_mg":        (TARGET_REACH, 8,    None, "mg"),   # RDA (male)
    "magnesium_mg":   (TARGET_REACH, 400,  None, "mg"),   # RDA (male 19-30)
    "zinc_mg":        (TARGET_REACH, 11,   None, "mg"),   # RDA
    "potassium_mg":   (TARGET_REACH, 3400, None, "mg"),   # AI (male, 2019 DRI)
    "phosphorus_mg":  (TARGET_REACH, 700,  None, "mg"),   # RDA
    "copper_mg":      (TARGET_REACH, 0.9,  None, "mg"),   # RDA
    "manganese_mg":   (TARGET_REACH, 2.3,  None, "mg"),   # AI (male)
    "selenium_ug":    (TARGET_REACH, 55,   None, "ug"),   # RDA
    "iodine_ug":      (TARGET_REACH, 150,  None, "ug"),   # RDA
    "omega3_g":       (TARGET_REACH, 1.6,  None, "g"),    # ALA AI (male)
    # things to stay under
    "sodium_mg":      (TARGET_LIMIT, None, 2300, "mg"),   # CDRR
    "trans_fat_g":    (TARGET_LIMIT, None, 2,    "g"),    # keep as low as possible
}

# The biological kinetics of each nutrient — the reference science that turns a bare
# daily percentage into an honest reading. Two intrinsic properties, same for every
# person, so they live here (not in the user's sheet) and are attached at read time by
# _with_kinetics:
#
#   horizon  daily | rolling (see HORIZON_*). Water-soluble vitamins are `daily`, BUT
#            the taxonomy follows physiology, not chemistry: B12 and folate are
#            `rolling` because the liver banks them (B12 for 3-5 YEARS, folate for
#            weeks), while magnesium/potassium/zinc are `daily` because an active male
#            sweats them out faster than any tissue pool can buffer.
#   ceiling  a Tolerable Upper Intake Level (UL), assigned ONLY when it is both
#            reachable from food/ordinary supplements AND genuinely harmful. This is
#            the line between a harmless surplus and a dangerous one: vitamin C's UL
#            (2000 mg) is ~22x its floor and unreachable from food, so a 300% day is
#            fine and it gets NO ceiling; iron's UL (45 mg) is one liver portion away
#            and a male cannot excrete the excess, so it gets one. A `None` here means
#            "surplus is safe" and the UI must never colour it as a risk.
#
# Anything absent from this map defaults to (daily, no ceiling). Only the `rolling`
# nutrients and the one `daily` nutrient with a reachable ceiling (zinc) are listed;
# the remaining daily-with-no-ceiling nutrients (vitamin C, B1/B2/B3/B5/B6, choline,
# magnesium, potassium, fibre, cholesterol) fall through to the
# default. The pure `limit` metrics (sodium, added sugar, sat/trans fat) already carry
# their ceiling from _MICRO_TARGETS/_derive_targets and just take the daily default.
# Cholesterol has neither a target ceiling nor a UL here (see the note above
# _MICRO_TARGETS) — it's tracked but never colours as a risk.
# Each entry: key -> (horizon, upper_limit | None).
_NUTRIENT_KINETICS: Dict[str, Tuple[str, Optional[float]]] = {
    # fat-soluble vitamins — stored in liver and fat for weeks to months.
    "vitamin_a_ug":   (HORIZON_ROLLING, 3000.0),   # preformed retinol is hepatotoxic
    "vitamin_e_mg":   (HORIZON_ROLLING, 1000.0),   # anticoagulant at megadoses
    # water-soluble but body-banked, so read as reserves, not day-to-day.
    "vitamin_b12_ug": (HORIZON_ROLLING, None),     # liver holds 3-5 years
    "folate_ug":      (HORIZON_ROLLING, None),     # UL is synthetic-only; food exempt
    # minerals the body accumulates / regulates over the long term.
    "iron_mg":        (HORIZON_ROLLING, 45.0),     # a male cannot excrete the excess
    "calcium_mg":     (HORIZON_ROLLING, 2500.0),   # a ~1 kg skeletal buffer
    "phosphorus_mg":  (HORIZON_ROLLING, 4000.0),   # bone-buffered; excess is the risk
    "copper_mg":      (HORIZON_ROLLING, 10.0),     # liver-stored
    "manganese_mg":   (HORIZON_ROLLING, 11.0),     # neurotoxic ceiling
    "selenium_ug":    (HORIZON_ROLLING, 400.0),    # narrow window (one Brazil nut!)
    "iodine_ug":      (HORIZON_ROLLING, 1100.0),   # thyroid banks 70-80%
    "omega3_g":       (HORIZON_ROLLING, None),     # incorporates into membranes slowly
    # non-cumulative, but the ONE daily nutrient with a reachable, risky ceiling.
    "zinc_mg":        (HORIZON_DAILY,   40.0),     # excess depletes copper
}

# Recomposition parameters (the user's chosen goal: lose fat, hold muscle). All
# derive from the user's OWN measured data so the targets track the real body, not
# a guess. Tunable here; a per-metric `source=manual` row in the sheet overrides
# any of them.
TDEE_WINDOW_DAYS = 14          # rolling average of measured total_cals_out
RECOMP_DEFICIT = 0.125         # calorie target centre: 12.5% below measured TDEE
CALORIE_WINDOW_HALF = 0.075    # window is centre ±7.5% of TDEE => a 5%-20% deficit
PROTEIN_G_PER_KG = 2.0         # hero metric: 2.0 g per kg body weight (~140 g)

# Per-goal energy and protein parameters. The goal is a SEPARATE axis from what the
# user measures (see schema/capabilities.py): owning a scale doesn't say what you
# are aiming at, and aiming at muscle gain doesn't require one. A negative deficit
# is a surplus.
#
# Protein rises as the deficit deepens, which is not arbitrary: protein is what
# decides whether the weight lost is fat or muscle, so the harder the cut the more
# of it is needed. `health` has no body-composition goal at all and just takes the
# adequacy figure.
_GOAL_PARAMS: Dict[str, Dict[str, float]] = {
    "recomposition": {"deficit": RECOMP_DEFICIT, "protein_g_per_kg": PROTEIN_G_PER_KG},
    "fat_loss":      {"deficit": 0.20,           "protein_g_per_kg": 2.2},
    "muscle_gain":   {"deficit": -0.10,          "protein_g_per_kg": 1.8},
    "maintenance":   {"deficit": 0.0,            "protein_g_per_kg": 1.6},
    "health":        {"deficit": 0.0,            "protein_g_per_kg": 1.2},
}
FAT_G_PER_KG = 0.8             # floor for hormonal health
FAT_CEILING_MULTIPLIER = 1.25  # ceiling = floor × 1.25 (~1.0 g/kg)
FIBER_G_PER_1000KCAL = 14.0    # standard fibre recommendation
LIMIT_ENERGY_FRACTION = 0.10   # added sugar & saturated fat ceilings: 10% of energy
# Fallbacks when the sheet has no measured history yet, so day one still has sane
# targets instead of zeros.
DEFAULT_TDEE = 2200.0
DEFAULT_WEIGHT_KG = 70.0
BMR_TO_TDEE = 1.45             # if only a scale BMR exists, lightly-active multiplier

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
singular English in `name`. If a packaged item shows a nutrition label, READ IT and
scale to the visible portion — labels beat estimation.

Also give every item a `name_pt`: the SAME food named in European Portuguese
(pt-PT, not Brazilian), lowercase, as a person in Portugal would say it at the
table — "peito de frango", "arroz branco", "queijo fresco". Rules:
  - If the user's note names the dish, reuse THEIR word exactly ("francesinha",
    "arroz de pato", "migas") — never re-translate what they already wrote, and
    set `name` to the closest English description of it.
  - Keep brands, proper nouns and terms Portuguese speakers normally say in
    English unchanged ("whey protein", "Big Tasty", "cottage cheese", "Pingo
    Doce") — a forced translation reads worse than the original.
  - Match `name`'s level of detail: "skin-on chicken thigh" -> "coxa de frango com
    pele", not just "frango".
  - If the Portuguese is identical to the English, repeat it; never leave it empty.

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
    zinc_mg, phosphorus_mg, copper_mg, manganese_mg, cholesterol_mg,
    choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg,
    vitamin_b3_mg, vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug):  vitamin_a_ug, vitamin_b12_ug, folate_ug, selenium_ug,
    iodine_ug
Do NOT report vitamin D, vitamin K, biotin or chloride — they are deliberately
not tracked (sun, gut bacteria and salt are their real sources, not the plate),
and any value for them is discarded.
Report EVERY nutrient key this food is a genuine dietary source of, however
small — a food doesn't need to be famous for a nutrient to contribute a
meaningful amount of it (even ~5% of a daily reference intake is worth
reporting). Most whole foods register on 10+ of these 32 keys; if you're
listing only 2-3, you're almost certainly under-reporting — go back through
the full list and check each one. OMIT a key only when the food is not a
plausible source of it at all (e.g. no B12/iodine from an all-plant item with
no iodized salt or seaweed) — never omit one just because it isn't the item's
defining nutrient. Base values on the cooked weight and method from steps 3-4.

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
("chicken with rice" = two items). Name items in lowercase singular English in
`name`.

Also give every item a `name_pt`: the SAME food in European Portuguese (pt-PT, not
Brazilian), lowercase, as a person in Portugal would say it. The description is
usually ALREADY in Portuguese — when it is, reuse the user's own words verbatim
("francesinha", "arroz de pato", "secreto de porco") and make `name` the English
description of that. Keep brands and terms normally said in English unchanged
("whey protein", "Big Tasty"). Match `name`'s level of detail. Never leave it
empty — if the Portuguese is identical to the English, repeat it.

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
    zinc_mg, phosphorus_mg, copper_mg, manganese_mg, cholesterol_mg,
    choline_mg, vitamin_c_mg, vitamin_e_mg, vitamin_b1_mg, vitamin_b2_mg,
    vitamin_b3_mg, vitamin_b5_mg, vitamin_b6_mg
  micrograms (ug):  vitamin_a_ug, vitamin_b12_ug, folate_ug, selenium_ug,
    iodine_ug
Do NOT report vitamin D, vitamin K, biotin or chloride — they are deliberately
not tracked (sun, gut bacteria and salt are their real sources, not the plate),
and any value for them is discarded.
Report every key this food is a genuine dietary source of, however small — a
food doesn't need to be famous for a nutrient to contribute a meaningful
amount of it (even ~5% of a daily reference intake is worth reporting). Omit a
key only when the food is not a plausible source of it at all.

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
                property_ordering=["name", "name_pt", "cooking_method",
                                   "portion_g", "calories", "protein_g",
                                   "carbs_g", "fat_g", "nutrients"],
                properties={
                    "name": types.Schema(type=types.Type.STRING),
                    # The pt-PT name the app displays. Written here, beside the
                    # English key, because this call is the only place that sees
                    # BOTH the photo and the user's own (Portuguese) note — so a
                    # dish they named themselves keeps their words instead of
                    # round-tripping through English and back.
                    "name_pt": types.Schema(type=types.Type.STRING),
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


def _trigger_daily_sync_local(day: str) -> None:
    """Kick the daily job on this machine instead of the Cloud Run Jobs API.

    `systemctl start` is a no-op on an already-active unit, so systemd absorbs the
    common double-trigger without us asking anything first — the equivalent of the
    executions poll the Cloud Run path does. It is NOT the real guard, though:
    `src.daily_runner` holds an flock, because the 11:00 timer and a manual run can
    collide in ways systemd's per-unit check would not catch.

    `--no-block` matters. Without it systemctl waits for the job to finish, and
    this runs on the queue worker's thread — a 60-second daily sync would hold the
    /process request open and burn the analysis budget.
    """
    unit = os.environ.get("DAILY_JOB_UNIT", "health-tracker-daily.service")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", "--no-block", unit],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL)
        if result.returncode == 0:
            app.logger.info("weigh-in for %s woke the daily sync (%s)", day, unit)
        else:
            app.logger.warning("daily sync trigger failed (%s): %s",
                               result.returncode, (result.stderr or "")[:200])
    except Exception:
        app.logger.exception("daily sync trigger failed (backstop will cover it)")


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
    if _queue_backend() == "local":
        _trigger_daily_sync_local(day)
        return

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


def _queue_backend() -> str:
    """Which queue implementation to use: "cloudtasks" (default) or "local".

    Selected by env rather than by import availability so that the two can run side
    by side during the migration off Cloud Run — the same image/checkout serves
    both, and rolling back is an env change plus a restart, not a redeploy.
    `localqueue` copies the `meal-ingest` queue's semantics exactly; see its
    docstring for the table."""
    return os.environ.get("QUEUE_BACKEND", "cloudtasks").strip().lower()


def _enqueue_process(payload: Dict[str, Any]) -> None:
    """Hand a meal to the background worker via the task queue. The queue retries
    the /process call with backoff for its whole window, so a transient model
    outage can't lose the meal. Raises if the queue isn't configured/reachable, so
    the caller can fall back to a stub (never worse than the old synchronous path).

    Images can't ride in the task (Cloud Tasks bodies are ~small), so the payload
    carries their Drive ids and the worker fetches the bytes back. The local queue
    inherits that shape rather than growing a second one — the worker's fetch path
    is the tested one."""
    body = json.dumps(payload).encode("utf-8")
    url = os.environ["PROCESS_URL"]
    headers = {"Content-Type": "application/json",
               "X-Auth-Token": os.environ.get("INGEST_TOKEN", "")}

    if _queue_backend() == "local":
        import localqueue  # lazy, mirroring the Cloud Tasks import below
        localqueue.enqueue(url, body, headers)
        return

    from google.cloud import tasks_v2  # lazy: keeps tests importable without the lib
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        os.environ["GCP_PROJECT"],
        os.environ.get("TASKS_LOCATION", "europe-west1"),
        os.environ["TASKS_QUEUE"],
    )
    client.create_task(parent=parent, task={"http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": url,
        "headers": headers,
        "body": body,
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
    """Coerce the model's item list into clean {name, name_pt?, portion_g, macros,
    cooking_method?, nutrients?} dicts.

    `name` is the English canonical key — FDC grounding, the food taxonomy and every
    aggregation key off it. `name_pt` is what the app shows. Kept only when the model
    actually gave something different: a name that is identical in both languages
    ("whey protein", a brand) needs no second copy, and the display layer falls back
    to `name` whenever `name_pt` is absent.
    """
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
        name_pt = str(entry.get("name_pt", "")).strip()[:120]
        if name_pt and name_pt.casefold() != name.casefold():
            item["name_pt"] = name_pt
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


def _meal_totals(items: List[Dict[str, Any]]) -> Dict[str, float]:
    """A meal row's flat columns = the sum of its items, for every numeric column a
    row carries. Shared by fresh ingest (_meal_from_items) and a user's later
    hand-correction (/meals/edit) so both derive totals the same way."""
    def total(key: str) -> float:
        return round(sum(i[key] for i in items), 1)

    return {
        "portion_g": total("portion_g"),
        "calories": total("calories"),
        "protein_g": total("protein_g"),
        "carbs_g": total("carbs_g"),
        "fat_g": total("fat_g"),
    }


def _meal_from_items(items: List[Dict[str, Any]], confidence: Any,
                     model: str) -> Dict[str, Any]:
    """Assemble the meal record (row totals = sum over items)."""
    return {
        "items": items,
        "foods": ", ".join(i["name"] for i in items) if items else "not food",
        **_meal_totals(items),
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
        if max(macros.values()) <= 0 and not _has_any_nutrients(row):
            continue
        for k, v in macros.items():
            totals[k] += v
    return {k: round(v, 1) for k, v in totals.items()}


def _is_stub(row: Dict[str, Any]) -> bool:
    return str(row.get("foods") or "").strip().lower() in NON_MEALS


def _has_any_nutrients(row: Dict[str, Any]) -> bool:
    """True when any per-ingredient item carries a non-zero micronutrient — so that
    zero-macro supplements (magnesium, vitamins, ...) survive the content filter and
    appear in the app and daily roll-up."""
    for item in _parse_items_cell(row.get("items")):
        nutrients = item.get("nutrients") or {}
        for v in nutrients.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return True
    return False


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


def _meal_row_index_by_datetime(values: List[List[Any]], when: str) -> Optional[int]:
    """1-based sheet row of the meal with this exact `datetime` (a meal's id, as
    shown to and sent back by the app), or None. Used by /meals/edit — unlike
    _meal_row_index this looks up by the meal's own identity, not a photo hash."""
    if not values:
        return None
    header = values[0]
    try:
        dt_i = header.index("datetime")
    except ValueError:
        return None
    for n, r in enumerate(values[1:], start=2):
        if len(r) > dt_i and str(r[dt_i]) == when:
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
            return _record_from(data, model, allow_body=allow_body,
                                allow_bowel=allow_bowel)
    raise RuntimeError(f"all models failed ({models}); last error: {last_err}")


def _record_from(data: Dict[str, Any], model: str, *, allow_body: bool = True,
                 allow_bowel: bool = False) -> Dict[str, Any]:
    """Turn a model's parsed JSON into the record the callers act on.

    The router fork: only a verdict the current path can legitimately produce is
    honoured; anything else is a meal — the common case and the safe default.

    Factored out of `_run_models` so the Claude path (see `claude_estimator`)
    assembles its record through the SAME code rather than a parallel copy.
    Normalisation is where the safety lives — `_normalize_body`'s plausibility
    bands are what stop a misread scale screenshot reaching the sheet — so a second
    implementation of this would be a second place for that to rot.
    """
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
        app.logger.info("%s classified the note as a bowel-movement log", model)
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


def _try_claude(prompt: str, images: Optional[List[Tuple[bytes, str]]] = None,
                *, allow_body: bool = True,
                allow_bowel: bool = False) -> Optional[Dict[str, Any]]:
    """Estimate with the local Claude CLI, or return None to fall through to Gemini.

    Returns None — never raises — on every failure mode: the estimator not being
    enabled (the cloud deployment), the CLI being absent, a spent 5-hour usage
    window, a timeout, an unparseable answer. Gemini stays wired behind it for
    exactly this reason: a spent window must cost the meal some accuracy, never the
    row itself.

    The Gemini slice the caller was given (`_worker_kwargs`) is untouched by this,
    so the escalation still works as documented — Claude is simply tried first on
    every attempt, and the queue's patience budget then plays out below it.
    """
    if not claude_estimator.enabled():
        return None
    try:
        data = claude_estimator.analyze(prompt, images)
    except Exception as err:
        # warning, not exception(): a spent usage window is an expected daily event
        # on a subscription, not a defect, and a stack trace per meal would bury the
        # real failures.
        app.logger.warning("claude estimate unavailable, falling back to Gemini: %s",
                           err)
        return None
    try:
        return _record_from(data, claude_estimator.model(),
                            allow_body=allow_body, allow_bowel=allow_bowel)
    except Exception:
        app.logger.exception("claude answered but the record could not be built")
        return None


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
    prompt = _build_prompt(len(images), note, now, templates)

    record = _try_claude(prompt, images=images, allow_body=True)
    if record is not None:
        return record

    parts: List[Any] = [types.Part.from_bytes(data=img, mime_type=mime)
                        for img, mime in images]
    parts.append(prompt)
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
    record = _try_claude(prompt, allow_body=False, allow_bowel=True)
    if record is not None:
        return record

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
    the first attempt would drop to flash-lite within seconds of a 3.6-flash
    hiccup and answer with the weaker model, and the remaining seven attempts —
    the patience — would never be used at all.

    So: every attempt but the last few calls ONLY the first model, retrying it
    within the attempt and then handing back a 5xx so Cloud Tasks re-runs us after
    a backoff. With the queue's 8 attempts and 5→120 s backoff that is ~30 shots
    at gemini-3.6-flash spread over ~11 minutes (measured) before anything weaker
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
        # Make the file publicly viewable so the iOS app can display it directly
        # via thumbnail URL (no auth required). File IDs are random UUIDs, so
        # this is effectively private-by-obscurity for meal photos.
        fid = created.get("id", "")
        if fid:
            _execute(lambda fid=fid: _drive().permissions().create(
                fileId=fid, body={"type": "anyone", "role": "reader"},
            ))
        out.append({"id": fid,
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


def _all_meal_rows() -> List[Dict[str, Any]]:
    """Every meal row as a dict, or [] if the tab doesn't exist yet. Read once per
    request and sliced for both today and the rolling history, so the window costs no
    extra sheet read (the whole meals tab is already loaded)."""
    try:
        return _rows_as_dicts(_read_tab(MEALS_TAB))
    except Exception:  # tab not created yet
        return []


def _todays_meals(today: str) -> List[Dict[str, Any]]:
    return [r for r in _all_meal_rows()
            if str(r.get("datetime", "")).startswith(today)]


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


def _heal_daily_duplicates(grid: List[List[Any]]) -> List[List[Any]]:
    """Collapse rows that share a date, folding the extras' non-blank cells into
    the first occurrence and deleting them from the sheet.

    This service and `src/run_daily.py`'s job are two independent read-modify-write
    writers against the same tab with no lock between them: a weigh-in here writes
    the body columns for TODAY the instant it lands, but if the daily job's grid
    snapshot was taken a moment earlier (it was already mid-run — the 11:00
    backstop, or a previous weigh-in's triggered run still going) it won't find
    that date yet and appends a second, half-empty row instead of merging into it.
    `_trigger_daily_sync` already refuses to start a SECOND overlapping run of that
    job, but it can't stop this race against a run that was already in flight.
    Healing on every write means the duplicate never survives past the next call
    here, rather than accumulating."""
    header = grid[0] if grid else []
    width = len(header)
    if len(grid) < 3:
        return grid

    survivor_idx: Dict[str, int] = {}    # date -> index into `grid`
    survivors: Dict[int, List[Any]] = {}  # grid index -> merged, padded row
    doomed_rownums: List[int] = []       # 1-based sheet rows to delete

    for i in range(1, len(grid)):
        row = grid[i]
        if not row:
            continue
        day = str(row[0])
        padded = list(row) + [None] * (width - len(row))
        if day not in survivor_idx:
            survivor_idx[day] = i
            survivors[i] = padded
        else:
            target = survivors[survivor_idx[day]]
            for col in range(1, width):
                if target[col] in (None, "") and padded[col] not in (None, ""):
                    target[col] = padded[col]
            doomed_rownums.append(i + 1)  # grid[0] is sheet row 1 (the header)

    if not doomed_rownums:
        return grid

    app.logger.warning("%s: healing %d duplicate-date row(s)",
                        DAILY_TAB, len(doomed_rownums))

    data = [
        {"range": f"{DAILY_TAB}!A{i + 1}:{_col_letter(width - 1)}{i + 1}",
         "values": [survivors[i]]}
        for i in survivor_idx.values()
    ]
    _execute(lambda: _sheets().spreadsheets().values().batchUpdate(
        spreadsheetId=_sid(), body={"valueInputOption": "RAW", "data": data}))

    tab_id = _tab_id(DAILY_TAB)
    if tab_id is not None:
        # One batch, indices descending, so deleting a lower row never shifts the
        # sheet row number a later request in the same batch still refers to.
        delete_requests = [
            {"deleteDimension": {"range": {
                "sheetId": tab_id, "dimension": "ROWS",
                "startIndex": rownum - 1, "endIndex": rownum,
            }}}
            for rownum in sorted(doomed_rownums, reverse=True)
        ]
        _execute(lambda: _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(), body={"requests": delete_requests}))

    return [header] + [survivors[i] for i in sorted(survivor_idx.values())]


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

    grid = _heal_daily_duplicates(grid)

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


# -- targets: derive, read, merge, seed ----------------------------------------
def _to_float(value: Any) -> Optional[float]:
    """A sheet cell as a float, or None for blank/non-numeric. Unlike _round_num
    (which clamps meal macros to >=0) this keeps the difference between a real 0 and
    a missing value — the target derivation and the app both depend on it."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _recent_average(rows: List[Dict[str, Any]], key: str, n: int) -> Optional[float]:
    """Mean of the last `n` positive values of `key`. Rows arrive in sheet order
    (ascending date), so the tail is the most recent — this is the rolling TDEE."""
    values = [v for v in (_to_float(r.get(key)) for r in rows)
              if v is not None and v > 0]
    values = values[-n:]
    return sum(values) / len(values) if values else None


def _latest(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    """The most recent positive value of `key` (e.g. the last weigh-in)."""
    for row in reversed(rows):
        value = _to_float(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def _derive_targets(daily_rows: List[Dict[str, Any]],
                    caps: Optional["caps_mod.Capabilities"] = None
                    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The macro targets computed from the best data this user actually has.
    Returns (targets, basis) where `basis` is the inputs the numbers came from, so
    the app can show them honestly.

    Energy: a rolling TDEE (measured total_cals_out) adjusted by the goal's calorie
    offset, as a soft window. Protein/fat scale with body weight; carbs fill the
    remaining energy; fibre scales with the calorie target; the added-sugar and
    saturated-fat ceilings are 10% of energy each.

    **The source ladder is the whole point of the `caps` argument.** Each input
    takes the best available layer and says which one it used:

        measured  a real total_cals_out average / a real weigh-in
        declared  what the user typed into the `config` tab (Mifflin-St Jeor for
                  energy) — the only layer a friend with no watch and no scale has
        default   the built-in constants, which describe nobody in particular

    Without the declared layer a phone-only user got a hard-coded 2200 kcal and a
    70 kg protein target no matter who they were; with it, the same code path
    produces a target for their actual body. `basis.sources` records the choice per
    input, so the app can label a number "medido" or "declarado" rather than
    implying a measurement that never happened.
    """
    caps = caps or caps_mod.FULL
    goal = _GOAL_PARAMS.get(caps.goal, _GOAL_PARAMS[caps_mod.DEFAULT_GOAL])
    sources: Dict[str, str] = {}

    tdee = _recent_average(daily_rows, "total_cals_out", TDEE_WINDOW_DAYS) \
        if caps.has("activity") else None
    if tdee is not None:
        sources["tdee"] = SRC_MEASURED
    else:
        bmr = _latest(daily_rows, "bmr_kcal") if caps.has("body") else None
        declared = caps.declared_tdee()
        if bmr:
            tdee, sources["tdee"] = bmr * BMR_TO_TDEE, SRC_MEASURED
        elif declared is not None:
            tdee, sources["tdee"] = declared, SRC_DECLARED
        else:
            tdee, sources["tdee"] = DEFAULT_TDEE, SRC_DEFAULT

    weight = _latest(daily_rows, "weight_kg") if caps.has("body") else None
    if weight is not None:
        sources["weight"] = SRC_MEASURED
    elif caps.declared_weight_kg is not None:
        weight, sources["weight"] = caps.declared_weight_kg, SRC_DECLARED
    else:
        weight, sources["weight"] = DEFAULT_WEIGHT_KG, SRC_DEFAULT
    lean = _latest(daily_rows, "lean_mass_kg") if caps.has("body") else None

    deficit, protein_per_kg = goal["deficit"], goal["protein_g_per_kg"]
    centre = tdee * (1 - deficit)
    cal_floor = tdee * (1 - deficit - CALORIE_WINDOW_HALF)
    cal_ceil = tdee * (1 - deficit + CALORIE_WINDOW_HALF)
    protein = protein_per_kg * weight
    fat = FAT_G_PER_KG * weight
    carbs = max(0.0, (centre - 4 * protein - 9 * fat) / 4)
    fiber = FIBER_G_PER_1000KCAL * centre / 1000
    added_sugar = LIMIT_ENERGY_FRACTION * centre / 4
    sat_fat = LIMIT_ENERGY_FRACTION * centre / 9

    r10 = lambda x: float(round(x / 10) * 10)   # calories, to the nearest 10
    rg = lambda x: float(round(x))              # grams, to the nearest 1

    targets = {
        "calories": {"kind": TARGET_WINDOW, "floor": r10(cal_floor),
                     "ceiling": r10(cal_ceil), "unit": "kcal", "source": SRC_MEASURED},
        "protein_g": {"kind": TARGET_REACH, "floor": rg(protein), "unit": "g",
                      "source": SRC_MEASURED},
        "fat_g": {"kind": TARGET_WINDOW, "floor": rg(fat),
                  "ceiling": rg(fat * FAT_CEILING_MULTIPLIER), "unit": "g",
                  "source": SRC_MEASURED},
        "carbs_g": {"kind": TARGET_WINDOW, "floor": rg(carbs * 0.9),
                    "ceiling": rg(carbs * 1.1), "unit": "g", "source": SRC_MEASURED},
        "fiber_g": {"kind": TARGET_REACH, "floor": rg(fiber), "unit": "g",
                    "source": SRC_MEASURED},
        "added_sugar_g": {"kind": TARGET_LIMIT, "ceiling": rg(added_sugar),
                          "unit": "g", "source": SRC_MEASURED},
        "saturated_fat_g": {"kind": TARGET_LIMIT, "ceiling": rg(sat_fat),
                            "unit": "g", "source": SRC_MEASURED},
    }
    basis = {
        "tdee_kcal": rg(tdee),
        "calorie_target_kcal": r10(centre),
        "weight_kg": round(weight, 2),
        "lean_mass_kg": round(lean, 2) if lean is not None else None,
        "protein_g_per_kg": protein_per_kg,
        "calorie_deficit_pct": round(deficit * 100, 1),
        "goal": caps.goal,
        "goal_label_pt": caps_mod.GOAL_LABELS_PT.get(caps.goal, caps.goal),
        # Which layer each input came from, so nothing is ever presented as
        # measured when it was typed into a config tab.
        "sources": sources,
    }
    return targets, basis


def _micro_target_dict() -> Dict[str, Dict[str, Any]]:
    """The static RDA/limit reference table (adult male 19-50) as target dicts."""
    out: Dict[str, Dict[str, Any]] = {}
    for key, (kind, floor, ceiling, unit) in _MICRO_TARGETS.items():
        target: Dict[str, Any] = {"kind": kind, "unit": unit, "source": SRC_RDA}
        if floor is not None:
            target["floor"] = float(floor)
        if ceiling is not None:
            target["ceiling"] = float(ceiling)
        out[key] = target
    return out


def _targets_from_grid(values: Optional[List[List[Any]]]
                       ) -> Dict[str, Dict[str, Any]]:
    """Parse the `targets` tab into metric -> target dict. These are the rows the
    user can see and edit; a blank floor/ceiling is simply omitted.

    `None` (the tab could not be read) yields {}, so `_resolve_targets` falls back to
    the RDA defaults plus the live measured macros — complete and correct, just
    without the user's own edits for that one request. Note this is last-wins on
    duplicate metric rows, which is what kept the app behaving correctly while the
    live tab silently accumulated 11 copies of everything.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in _rows_as_dicts(values or []):
        metric = str(row.get("metric") or "").strip()
        if not metric:
            continue
        target: Dict[str, Any] = {
            "kind": str(row.get("kind") or "").strip() or TARGET_REACH,
            "unit": str(row.get("unit") or "").strip(),
            "source": str(row.get("source") or "").strip() or SRC_RDA,
        }
        floor = _to_float(row.get("floor"))
        ceiling = _to_float(row.get("ceiling"))
        if floor is not None:
            target["floor"] = floor
        if ceiling is not None:
            target["ceiling"] = ceiling
        out[metric] = target
    return out


def _resolve_targets(derived: Dict[str, Dict[str, Any]],
                     tab: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The full target set the app sees, layered so completeness and freshness are
    both guaranteed regardless of whether the sheet has been seeded yet:

      1. the RDA reference defaults (so every micro is always present);
      2. the tab rows on top (the user's edits / manual overrides / any snapshot);
      3. the measured macros, computed live, on top of that — EXCEPT where the user
         pinned a metric with source=manual, which always wins.
    """
    final: Dict[str, Dict[str, Any]] = _micro_target_dict()
    final.update(tab)
    for metric, target in derived.items():
        if tab.get(metric, {}).get("source") == SRC_MANUAL:
            continue
        final[metric] = target
    return final


def _with_kinetics(targets: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Attach each metric's intrinsic biology to its target: a `horizon` (daily vs
    rolling) and, for a nutrient with a reachable toxicity ceiling (a UL) that has no
    ceiling yet, that UL as `ceiling`.

    This is the last layer over _resolve_targets, applied only at the /today boundary
    so the layering logic and its tests stay untouched. It is purely additive — a
    user's own floor/ceiling (from the sheet) is never overwritten — and it is where
    the app learns that a low vitamin-D day is fine (rolling) while an iron surplus is
    not (a 45 mg ceiling)."""
    out: Dict[str, Dict[str, Any]] = {}
    for metric, target in targets.items():
        horizon, upper = _NUTRIENT_KINETICS.get(metric, (HORIZON_DAILY, None))
        enriched = {**target, "horizon": horizon}
        if upper is not None and enriched.get("ceiling") is None:
            enriched["ceiling"] = float(upper)
        out[metric] = enriched
    return out


def _todays_consumed(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """Live totals for the day: macros from the flat meal columns plus every one of
    the 37 micronutrients, summed from each meal's per-ingredient `items` JSON.
    Skips the same rows the totals do — stubs and zero-content rows — so consumed,
    the meal list and the daily roll-up all agree. A micro is emitted only when
    it's non-zero (a trace-free food shouldn't read as a hard zero)."""
    macro_keys = ("calories", "protein_g", "carbs_g", "fat_g")
    totals = {k: 0.0 for k in macro_keys}
    nutrients = {k: 0.0 for k in NUTRIENT_KEYS}
    for row in rows:
        if _is_stub(row):
            continue
        macros = {k: _round_num(row.get(k)) for k in macro_keys}
        if max(macros.values()) <= 0 and not _has_any_nutrients(row):
            continue
        for k in macro_keys:
            totals[k] += macros[k]
        for item in _parse_items_cell(row.get("items")):
            item_nutrients = item.get("nutrients") or {}
            for k in NUTRIENT_KEYS:
                value = item_nutrients.get(k)
                if isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and value > 0:
                    nutrients[k] += value
    consumed = {k: round(totals[k], 1) for k in macro_keys}
    for key, value in nutrients.items():
        if value > 0:
            consumed[key] = round(value, 2 if key.endswith("_g") else 1)
    return consumed


def _history_window(meal_rows: List[Dict[str, Any]], ref_day: str,
                    n: int) -> List[Dict[str, Any]]:
    """The `n` completed days BEFORE ref_day, each as {date, consumed} — the same
    per-day intake sum /today reports for today, computed live from the meals tab for
    the whole window. This is what lets the app read a buffered (rolling) nutrient
    against its multi-day average instead of alarming over a single low day.

    A day with no logged meal is OMITTED, never emitted as a zero: a blank day means
    'not tracked', and counting it as 0 intake would falsely drag a rolling average
    down. Oldest day first, so the app reads it left-to-right as time."""
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for row in meal_rows:
        day = str(row.get("datetime", ""))[:10]
        if day:
            by_day.setdefault(day, []).append(row)
    ref = datetime.fromisoformat(ref_day).date()
    out: List[Dict[str, Any]] = []
    for delta in range(n, 0, -1):
        day = (ref - timedelta(days=delta)).isoformat()
        rows = by_day.get(day)
        if not rows:
            continue
        consumed = _todays_consumed(rows)
        if consumed.get("calories", 0) > 0:   # a real eating day, not just stubs
            out.append({"date": day, "consumed": consumed})
    return out


# -- pt-PT display names --------------------------------------------------------
# The sheet stores English: `items[].name` is the key FDC grounding, the food
# taxonomy and every aggregation are written against. The app is Portuguese
# (Portugal) throughout, so the API translates on the way out.
#
# Nothing is translated HERE, though — resolving a name is a pure table lookup in
# `food_taxonomy.display_pt`, fed by the meal's own `name_pt` (written at ingest,
# against the photo and the user's note) and by the curated + learned lexicon. No
# model call, no write, no failure mode: a name nothing can place shows in English,
# which is what it did before this existed.

# The taxonomy blob lives in GCS and is read on every coach run. Re-reading it per
# request would put a network round trip on the app's main screen for a table that
# changes only when the coach learns a new food (rare, and increasingly never), so
# it's held for a minute. A stale read costs one meal one English name for 60 s.
_TAXONOMY_TTL_S = 60
_taxonomy_cached: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)


def _display_taxonomy() -> Optional[Dict[str, Any]]:
    """The taxonomy blob for display lookups, cached briefly. Returns None rather
    than raising — `display_pt` treats that as "curated table only"."""
    global _taxonomy_cached
    cached_at, blob = _taxonomy_cached
    now = time.time()
    if blob is not None and now - cached_at < _TAXONOMY_TTL_S:
        return blob
    try:
        store = _coach("coach_store")
        blob = store.read_json(store.TAXONOMY, default=None)
    except Exception:
        app.logger.warning("taxonomy read for display failed (non-fatal)",
                           exc_info=True)
        blob = None
    _taxonomy_cached = (now, blob)
    return blob


def _display_items(items: List[Dict[str, Any]],
                   taxonomy: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`items` with each `name` replaced by its pt-PT display name.

    `name_pt` is consumed here rather than passed through: the app renders one name
    per item, and every client-side path (the drill-down, the nutrient attribution,
    the share sheet) should show the same string without having to know which field
    to prefer.
    """
    tax = _coach("food_taxonomy")
    out: List[Dict[str, Any]] = []
    for item in items:
        item = dict(item)
        item["name"] = tax.display_pt(item.get("name", ""), taxonomy,
                                      name_pt=item.pop("name_pt", None))
        out.append(item)
    return out


def _display_foods(items: List[Dict[str, Any]], fallback: str) -> str:
    """The meal's one-line food list, rebuilt from its already-translated items.

    Deliberately NOT a translation of the stored `foods` cell. That cell is itself
    just ", ".join(item names) (see _meal_from_items), and item names legitimately
    contain commas — "chicken thigh, skin-on" is an example in the ingest prompt —
    so splitting it back apart would shatter such a name into fragments that match
    nothing. Rebuilding from the items is exact. `fallback` covers a legacy row
    whose items cell is empty but whose `foods` text isn't.
    """
    return ", ".join(str(i.get("name", "")).strip()
                     for i in items if str(i.get("name", "")).strip()) or fallback


def _today_meals_out(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The day's meals for /today: the same shape as /meals but WITH each meal's
    per-ingredient `items` (each carrying its `nutrients` map), so the app can show,
    for any nutrient, exactly which foods contributed it — the drill-down feature —
    without a second request."""
    macro_keys = ("calories", "protein_g", "carbs_g", "fat_g")
    taxonomy = _display_taxonomy()
    out: List[Dict[str, Any]] = []
    for row in rows:
        if _is_stub(row):
            continue
        macros = {k: _round_num(row.get(k)) for k in macro_keys}
        if max(macros.values()) <= 0 and not _has_any_nutrients(row):
            continue
        when = str(row.get("datetime") or "")
        items = _display_items(
            _normalize_items(_parse_items_cell(row.get("items"))), taxonomy)
        out.append({
            "datetime": when,
            "time": when[11:16],
            "foods": _display_foods(items, str(row.get("foods") or "").strip()),
            "note": str(row.get("note") or "").strip(),
            "template": str(row.get("template") or "").strip(),
            "photo_url": str(row.get("photo_url") or "").strip(),
            "edited": bool(str(row.get("edited_at") or "").strip()),
            **macros,
            "items": items,
        })
    out.sort(key=lambda m: m["datetime"])
    return out


# -- capabilities: which blocks this user actually measures ---------------------
# One switch, read from the sheet, that decides what the API serves, what the app
# draws and which domains the coach may speak about. See schema/capabilities.py for
# why it is a set of blocks rather than a level. Cached briefly because every
# request needs it and it changes about once in the life of a deployment.
_CAPS_TTL_S = 120
_caps_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _read_config_grid() -> List[List[Any]]:
    """The raw `config` tab, or [] if it hasn't been created yet."""
    try:
        return _read_tab(CONFIG_TAB)
    except Exception:
        return []


def _ensure_config_tab() -> None:
    meta = _execute(lambda: _sheets().spreadsheets().get(spreadsheetId=_sid()))
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if CONFIG_TAB not in titles:
        _execute(lambda: _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(),
            body={"requests": [{"addSheet": {
                "properties": {"title": CONFIG_TAB}}}]}))
    headers = list(caps_mod.CONFIG_TAB_HEADERS)
    last_col = chr(ord("A") + len(headers) - 1)
    rng = f"{CONFIG_TAB}!A1:{last_col}1"
    current = _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=rng)).get("values", [[]])
    if not current or current[0] != headers:
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{CONFIG_TAB}!A1",
            valueInputOption="RAW", body={"values": [headers]}))


def _seed_config(grid: List[List[Any]]) -> None:
    """Materialise any missing config key into the sheet, exactly as `_seed_targets`
    does for targets. Idempotent, so it is cheap to call on every read, and the
    result is that a fresh sheet gains an editable, self-documenting description of
    what this user measures instead of an empty grid."""
    existing = {str(r.get("key") or "").strip().lower()
                for r in _rows_as_dicts(grid)}
    rows = [list(seed) for seed in caps_mod.CONFIG_SEED
            if seed[0] not in existing]
    if not rows:
        return
    _ensure_config_tab()
    _execute(lambda: _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{CONFIG_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}))


def _capabilities() -> "caps_mod.Capabilities":
    """This user's capability, from the `config` tab.

    Never fatal and never restrictive on failure: a missing tab, a permissions blip
    or a typo all fall back to `caps_mod.FULL`. Hiding a block the user really has
    would look like data loss, which is far worse than showing an empty one.
    """
    now = time.time()
    if _caps_cache["value"] is not None \
            and now - float(_caps_cache["at"]) < _CAPS_TTL_S:
        return _caps_cache["value"]
    try:
        grid = _read_config_grid()
        try:  # materialise the defaults on first run; never fatal
            _seed_config(grid)
        except Exception:
            app.logger.warning("config seed skipped (non-fatal)", exc_info=True)
        caps = caps_mod.from_config(_rows_as_dicts(grid))
    except Exception:
        app.logger.warning("config read failed; assuming full capabilities",
                           exc_info=True)
        caps = caps_mod.FULL
    _caps_cache.update({"at": now, "value": caps})
    return caps


def _read_targets_grid() -> Optional[List[List[Any]]]:
    """The raw `targets` tab, or **None if it could not be read**.

    None and [] must stay distinguishable, and this is not a style point — conflating
    them is what corrupted the tab once already. `_seed_targets` treats "no rows" as
    "nothing is seeded yet" and appends the ENTIRE metric set. So when a transient
    Sheets error (a 429, a 503, a network blip) was swallowed into `[]`, the next
    read re-seeded all ~36 metrics on top of the ones already there. Ten such blips
    left the live tab with 11 duplicate copies of every metric and 386 rows.

    It stayed invisible because `_targets_from_grid` does `out[metric] = ...` in row
    order — last-wins — so the app kept reading the newest copy and behaved
    correctly while the tab silently grew.

    Callers must treat None as "unknown, change nothing"; only [] may be seeded.
    """
    try:
        return _read_tab(TARGETS_TAB)
    except Exception:
        app.logger.warning("targets tab unreadable — skipping seed this run",
                           exc_info=True)
        return None


def _ensure_targets_tab() -> None:
    meta = _execute(lambda: _sheets().spreadsheets().get(spreadsheetId=_sid()))
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if TARGETS_TAB not in titles:
        _execute(lambda: _sheets().spreadsheets().batchUpdate(
            spreadsheetId=_sid(),
            body={"requests": [{"addSheet": {
                "properties": {"title": TARGETS_TAB}}}]}))
    rng = f"{TARGETS_TAB}!A1:{TARGETS_LAST_COL}1"
    current = _execute(lambda: _sheets().spreadsheets().values().get(
        spreadsheetId=_sid(), range=rng)).get("values", [[]])
    if not current or current[0] != TARGETS_TAB_HEADERS:
        _execute(lambda: _sheets().spreadsheets().values().update(
            spreadsheetId=_sid(), range=f"{TARGETS_TAB}!A1",
            valueInputOption="RAW", body={"values": [TARGETS_TAB_HEADERS]}))


def _target_seed_rows(existing: set,
                      derived: Dict[str, Dict[str, Any]]) -> List[List[Any]]:
    """Rows to APPEND so every known metric has a row, without touching a metric the
    user already has (their edits are law). Micros seed as `rda`; the measured
    macros seed as a snapshot — the live value is recomputed on every read anyway,
    so the snapshot is just there to make the sheet self-explanatory and editable."""
    seed = {**_micro_target_dict(), **derived}
    rows: List[List[Any]] = []
    for metric, target in seed.items():
        if metric in existing:
            continue
        rows.append([metric, target.get("kind", ""),
                     target.get("floor", ""), target.get("ceiling", ""),
                     target.get("unit", ""), target.get("source", "")])
    return rows


def _seed_targets(grid: Optional[List[List[Any]]],
                  derived: Dict[str, Dict[str, Any]]) -> None:
    """Materialise any missing metric rows into the sheet. Idempotent: once every
    metric has a row this appends nothing, so it's cheap to call on each read.

    A `grid` of None means the tab could not be read (see `_read_targets_grid`). That
    is NOT an empty tab, and seeding on it would append a full duplicate set of every
    metric — the exact bug that grew the live tab to 11 copies. Do nothing instead:
    a missed seed is fixed by the next successful read, a spurious one is permanent.
    """
    if grid is None:
        return
    existing = {str(r.get("metric") or "").strip() for r in _rows_as_dicts(grid)}
    rows = _target_seed_rows(existing, derived)
    if not rows:
        return
    _ensure_targets_tab()
    _execute(lambda: _sheets().spreadsheets().values().append(
        spreadsheetId=_sid(), range=f"{TARGETS_TAB}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}))


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


@app.get("/meal-photo/<file_id>")
def meal_photo(file_id: str):
    """Proxy a meal photo from Drive so the iOS app's AsyncImage can display
    it without needing to authenticate against Google directly."""
    try:
        meta = _execute(lambda fid=file_id: _drive().files().get(
            fileId=fid, fields="mimeType"))
        data = _execute(lambda fid=file_id: _drive().files().get_media(
            fileId=fid))
        return Response(data, mimetype=meta.get("mimeType", "image/jpeg"))
    except Exception:
        abort(404)


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
    # The day just changed, so the coach's time-sensitive cards ("what to eat next",
    # the afternoon check-in) are now describing a day that no longer exists.
    # Enqueue a regeneration rather than doing it here: this request is a Cloud Tasks
    # worker that must return promptly, and the refresh must never be able to fail a
    # meal that is already written.
    _trigger_coach_refresh("meal_logged")

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

    # Capability first, request second. A block this user doesn't measure is not
    # served even if a stale client asks for it by name — the switch has to be
    # enforced where the data leaves, not where it is drawn, or an old build would
    # keep rendering empty sleep charts for someone with no tracker.
    caps = _capabilities()
    known = [b for b in BLOCKS if b not in ("key", "meta")]
    wanted = [b for b in known if caps.has(b)]
    if request.args.get("blocks"):
        asked = [b.strip() for b in request.args["blocks"].split(",") if b.strip()]
        unknown = [b for b in asked if b not in known]
        if unknown:
            return jsonify({"error": f"unknown block(s) {unknown}",
                            "known": known}), 400
        wanted = [b for b in asked if caps.has(b)]

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
                    "blocks": wanted, "days": days,
                    "capabilities": caps.to_api()}), 200


# Item fields a hand correction may overwrite. portion_g is informational only in
# v1 — editing it does not rescale the other fields, it's just another number the
# user can type a correction into.
_EDITABLE_ITEM_FIELDS = ("calories", "protein_g", "carbs_g", "fat_g", "portion_g")


@app.post("/meals/edit")
def edit_meal():
    """Hand-correct one ingredient of an already-logged meal (e.g. the AI
    overestimated a food's protein). The meal's row totals are re-derived from its
    items afterwards (_meal_totals) so they never drift from what the item list
    actually adds up to.

    Only the touched columns are written (items + the 5 macro/portion totals +
    edited_at) — model, image_sha, photo_url, note and template are left completely
    alone, so this can never resurrect a stub or break de-duplication.

    Stamps `edited_at`, which marks the row so the local audit job
    (nutrition-audit/audit.py) skips it instead of overwriting the correction with a
    fresh photo re-estimate next time it runs.

    Body: {"datetime": "<meal id, as returned by /today>", "item_index": <int>,
           "calories"?, "protein_g"?, "carbs_g"?, "fat_g"?, "portion_g"?}
    — each numeric field is optional; only the ones given change.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    when = str(body.get("datetime") or "").strip()
    if not when:
        return jsonify({"error": "datetime is required"}), 400
    try:
        item_index = int(body.get("item_index"))
    except (TypeError, ValueError):
        return jsonify({"error": "item_index must be an integer"}), 400

    values = _read_tab(MEALS_TAB)
    rownum = _meal_row_index_by_datetime(values, when)
    if rownum is None:
        return jsonify({"error": f"no meal at datetime={when}"}), 404
    row = dict(zip(values[0], values[rownum - 1]))

    items = _parse_items_cell(row.get("items"))
    if not 0 <= item_index < len(items):
        return jsonify({"error": f"item_index {item_index} out of range "
                                  f"(meal has {len(items)} item(s))"}), 400

    for key in _EDITABLE_ITEM_FIELDS:
        if key in body:
            items[item_index][key] = _round_num(body[key])
    items = _normalize_items(items)

    updates: Dict[str, Any] = {
        "items": json.dumps(items, ensure_ascii=False),
        **_meal_totals(items),
        "edited_at": datetime.now(_tz()).isoformat(timespec="seconds"),
    }
    data = [
        {"range": f"{MEALS_TAB}!{_col_letter(MEALS_HEADERS.index(col))}{rownum}",
         "values": [[value]]}
        for col, value in updates.items()
    ]
    _execute(lambda: _sheets().spreadsheets().values().batchUpdate(
        spreadsheetId=_sid(), body={"valueInputOption": "RAW", "data": data}))

    meal_out = _today_meals_out([{**row, **updates}])
    return jsonify(meal_out[0] if meal_out else {"datetime": when, **updates}), 200


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
    taxonomy = _display_taxonomy()
    meals_out: List[Dict[str, Any]] = []
    for r in rows:
        if _is_stub(r):
            continue
        macros = {k: _round_num(r.get(k)) for k in macro_keys}
        if max(macros.values()) <= 0 and not _has_any_nutrients(r):
            continue
        when = str(r.get("datetime") or "")
        # The items are parsed only to rebuild `foods` in pt-PT — they are not part
        # of this response (that is /today's job), so they are dropped afterwards.
        items = _display_items(
            _normalize_items(_parse_items_cell(r.get("items"))), taxonomy)
        meals_out.append({
            "datetime": when,
            "time": when[11:16],  # "HH:MM" off the ISO string
            "foods": _display_foods(items, str(r.get("foods") or "").strip()),
            "note": str(r.get("note") or "").strip(),
            "template": str(r.get("template") or "").strip(),
            **macros,
        })
    meals_out.sort(key=lambda m: m["datetime"])

    return jsonify({"date": day, "count": len(meals_out),
                    "totals": _day_totals(rows), "meals": meals_out}), 200


@app.get("/today")
def today():
    """Everything the live daily screen needs, in one call: what's been eaten so
    far today, the target for every metric, and the meals — with per-ingredient
    nutrients for the food drill-down.

    Computed LIVE from today's `meals` rows, never from daily_summary: the roll-up
    there is written only once a day is over (see the schema), so it is always blank
    for the day in progress. `consumed` therefore sums the meal rows the same way
    the list and totals do, extended to all 37 micronutrients.

    `targets` merges three layers (see _resolve_targets): the RDA reference table,
    the user's edits in the `targets` tab, and the calorie/macro targets derived
    live from measured data, then each is stamped with its kinetics (`horizon` +, for
    a nutrient with a reachable toxicity ceiling, that UL — see _with_kinetics).
    `basis` exposes the inputs behind the derived numbers (TDEE, weight) so the app
    can be honest about where they came from. `history` is the last
    NUTRIENT_HISTORY_DAYS completed days of intake (same shape as `consumed`), so the
    app can read a buffered nutrient against its rolling average.

    `?date=` (default today, server tz) lets the app review an earlier day too.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    day = request.args.get("date") or datetime.now(_tz()).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    all_meal_rows = _all_meal_rows()                    # the meals tab, read once
    meal_rows = [r for r in all_meal_rows               # today: consumed + the list
                 if str(r.get("datetime", "")).startswith(day)]
    caps = _capabilities()
    daily_rows = _rows_as_dicts(_read_tab(DAILY_TAB))   # for the measured derivation
    derived, basis = _derive_targets(daily_rows, caps)

    grid = _read_targets_grid()
    try:  # materialise the defaults into the sheet on first run; never fatal
        _seed_targets(grid, derived)
    except Exception:
        app.logger.warning("targets seed skipped (non-fatal)", exc_info=True)
    targets = _with_kinetics(_resolve_targets(derived, _targets_from_grid(grid)))

    meals_out = _today_meals_out(meal_rows)
    return jsonify({
        "date": day,
        "meal_count": len(meals_out),
        "consumed": _todays_consumed(meal_rows),
        "targets": targets,
        "basis": basis,
        "meals": meals_out,
        "history": _history_window(all_meal_rows, day, NUTRIENT_HISTORY_DAYS),
        # The app draws itself from this: which tabs and sections exist at all, and
        # what the goal is called. Rides along on a call the app already makes on
        # every launch, so the switch costs no extra round trip.
        "capabilities": caps.to_api(),
    }), 200


# The static per-nutrient reference knowledge base (what each vitamin/mineral does,
# food sources, deficiency/excess, tips). It is REFERENCE content, identical for
# every user — so it lives in the repo (version-controlled, reviewable) next to this
# module and is served verbatim, rather than in the user's data sheet. One row per
# nutrient keyed by NUTRIENT_KEYS; the app renders only the fields that are filled.
NUTRIENT_INFO_FILE = os.path.join(os.path.dirname(__file__), "nutrient_info.json")


@functools.lru_cache(maxsize=1)
def _nutrient_info() -> Dict[str, Any]:
    """Load and cache the nutrient knowledge base. Never fatal: a missing/invalid
    file just means the app shows 'em breve' for every nutrient."""
    try:
        with open(NUTRIENT_INFO_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        app.logger.exception("nutrient_info.json missing or invalid")
        return {"version": 0, "nutrients": {}}
    if not isinstance(data, dict) or not isinstance(data.get("nutrients"), dict):
        app.logger.warning("nutrient_info.json has no nutrients map")
        return {"version": 0, "nutrients": {}}
    return data


@app.get("/nutrients")
def nutrients():
    """The per-nutrient reference knowledge base for the deep-info screen. Static
    across users and days, so a client fetches it once and caches it. Same auth as
    everything else."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_nutrient_info()), 200


# =============================================================================
# Weekly insights & next-meal coach (Phase 2)
#
# The DETERMINISTIC analysis (the Diagnosis, the food vocabulary, the portion math)
# lives in ingest/insights.py and is served read-only here — this is the single
# source of truth for the nutrition science, reused instead of re-derived. The
# STRONG-model narration runs on the local Mac (automation/insights/), which fetches
# `/insights/diagnose` + `/insights/food-profile`, writes the narrative and plates to
# the `weekly_reports` / `next_meal` tabs, and those are what `/insights/weekly` and
# `/insights/next-meal` serve back. No model ever runs on a request path here.
# =============================================================================

WEEKLY_TAB = "weekly_reports"
NEXT_MEAL_TAB = "next_meal"
# The food vocabulary looks back four whole weeks — long enough to know what the user
# actually eats, short enough to reflect current habits.
PROFILE_WINDOW_DAYS = 28

NUTRIENT_POLICY_FILE = os.path.join(os.path.dirname(__file__), "nutrient_policy.json")


@functools.lru_cache(maxsize=1)
def _insights_mod():
    """The deterministic insights core, loaded by file path so it resolves the same
    whether main.py is imported as a package in the container or by file path in the
    test suite. Pure stdlib, so importing it is free and never fatal."""
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "insights.py")
    spec = importlib.util.spec_from_file_location("insights", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _nutrient_policy() -> Dict[str, Any]:
    """The 'genuine issue vs non-problem' rules. Never fatal: a missing/invalid file
    degrades to defaults-only (every nutrient judged on the built-in defaults)."""
    try:
        with open(NUTRIENT_POLICY_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        app.logger.exception("nutrient_policy.json missing or invalid")
        return {"defaults": {}, "nutrients": {}}
    return data if isinstance(data, dict) else {"defaults": {}, "nutrients": {}}


def _resolved_targets_and_basis() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The full target set (RDA + user edits + measured macros, stamped with kinetics)
    and its basis — exactly what `/today` computes, factored out so every insights
    endpoint reads the identical science."""
    daily_rows = _rows_as_dicts(_read_tab(DAILY_TAB))
    derived, basis = _derive_targets(daily_rows, _capabilities())
    targets = _with_kinetics(_resolve_targets(derived, _targets_from_grid(
        _read_targets_grid())))
    return targets, basis


def _window_meals(all_rows: List[Dict[str, Any]], start: str, end: str
                  ) -> List[Dict[str, Any]]:
    """Meal rows whose civil day falls in [start, end] (inclusive, ISO dates)."""
    return [r for r in all_rows if start <= str(r.get("datetime", ""))[:10] <= end]


def _diagnosis_for(ref_day: str, window_days: int) -> Dict[str, Any]:
    """The deterministic Diagnosis for the `window_days` completed days before
    `ref_day`, reusing the tested `_history_window` for the per-day intake and the raw
    window meals for attribution/coverage. `prev_days` is the window before it, so the
    Diagnosis can read each nutrient's week-over-week trend."""
    all_rows = _all_meal_rows()
    ref = datetime.fromisoformat(ref_day).date()
    start = (ref - timedelta(days=window_days)).isoformat()
    end = (ref - timedelta(days=1)).isoformat()
    days = _history_window(all_rows, ref_day, window_days)
    prev_ref = (ref - timedelta(days=window_days)).isoformat()
    prev_days = _history_window(all_rows, prev_ref, window_days)
    targets, basis = _resolved_targets_and_basis()
    return _insights_mod().build_diagnosis(
        ref_day=ref_day, window_days=window_days, days=days, prev_days=prev_days,
        window_meals=_window_meals(all_rows, start, end),
        targets=targets, basis=basis, policy=_nutrient_policy())


@app.get("/insights/diagnose")
def insights_diagnose():
    """The Diagnosis JSON for a window (default: the last 7 completed days). The local
    generator's data source, and the debug view for eyeballing the deterministic
    analysis against a real week before any model call. `?date=` reviews another week's
    end; `?window=` overrides the length."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    day = request.args.get("date") or datetime.now(_tz()).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        window = max(1, min(31, int(request.args.get("window", NUTRIENT_HISTORY_DAYS))))
    except (TypeError, ValueError):
        window = NUTRIENT_HISTORY_DAYS
    return jsonify(_diagnosis_for(day, window)), 200


@app.get("/insights/food-profile")
def insights_food_profile():
    """The user's food vocabulary over the last four weeks: what they eat, when, a
    typical portion, and each food's per-gram nutrient density. Powers both the swap
    suggestions and the next-meal portion math."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    all_rows = _all_meal_rows()
    ref = datetime.now(_tz()).date()
    start = (ref - timedelta(days=PROFILE_WINDOW_DAYS)).isoformat()
    profile = _insights_mod().build_food_profile(
        _window_meals(all_rows, start, ref.isoformat()), NUTRIENT_KEYS,
        _display_taxonomy())
    return jsonify({"generated_for": ref.isoformat(),
                    "window_days": PROFILE_WINDOW_DAYS, "foods": profile}), 200


@app.get("/insights/next-meal-context")
def insights_next_meal_context():
    """The deterministic inputs for a next-meal suggestion: the day's remaining budget,
    which nutrients are still short today, and — per shortfall — the foods the user
    already eats that are densest in it, with the gram range that would close the gap.
    The model turns these into palatable plates; the numbers are ours.

    When `?v2=1` is passed, returns the enhanced context (with today's meals and meal
    timing profile) for the dynamic next-slot AI generation."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    now = datetime.now(_tz())
    day = now.date().isoformat()
    all_rows = _all_meal_rows()
    today_rows = [r for r in all_rows if str(r.get("datetime", "")).startswith(day)]
    consumed = _todays_consumed(today_rows)
    targets, _ = _resolved_targets_and_basis()
    start = (now.date() - timedelta(days=PROFILE_WINDOW_DAYS)).isoformat()
    window_meals = _window_meals(all_rows, start, day)
    ins = _insights_mod()
    taxonomy = _display_taxonomy()
    profile = ins.build_food_profile(window_meals, NUTRIENT_KEYS, taxonomy)

    is_v2 = request.args.get("v2") == "1"
    if is_v2:
        ctx = ins.next_meal_context_v2(
            consumed=consumed, targets=targets,
            focus_key=request.args.get("focus") or None,
            food_profile=profile,
            today_rows=today_rows, window_meals=window_meals,
            window_days=PROFILE_WINDOW_DAYS,
            current_time=now.strftime("%H:%M"), taxonomy=taxonomy)
    else:
        ctx = ins.next_meal_context(
            consumed=consumed, targets=targets,
            focus_key=request.args.get("focus") or None,
            food_profile=profile, slot=ins._meal_slot(now.isoformat()))
    return jsonify(ctx), 200


def _read_cached_rows(tab: str) -> List[Dict[str, Any]]:
    """A derived/observation insights tab as dicts, or [] if it doesn't exist yet
    (the local generator hasn't run) — so the app degrades to a 'pending' state
    instead of erroring."""
    try:
        return _rows_as_dicts(_read_tab(tab))
    except Exception:
        return []


def _loads_or(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


@app.get("/insights/weekly")
def insights_weekly():
    """The latest cached weekly report for the app. Read-only — the strong model wrote
    it on the Mac. `status: pending` until the first Sunday run has landed."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    rows = [r for r in _read_cached_rows(WEEKLY_TAB) if str(r.get("week_start") or "")]
    if not rows:
        return jsonify({"status": "pending", "report": None}), 200
    latest = max(rows, key=lambda r: str(r.get("week_start")))
    return jsonify({
        "status": str(latest.get("status") or "generated"),
        "week_start": latest.get("week_start"),
        "generated_at": latest.get("generated_at"),
        "window_start": latest.get("window_start"),
        "window_end": latest.get("window_end"),
        "focus_key": latest.get("focus_key") or None,
        "prior_focus_key": latest.get("prior_focus_key") or None,
        "prior_focus_delta": _loads_or(latest.get("prior_focus_delta"), None),
        "coverage_note": latest.get("coverage_note") or "",
        "report": _loads_or(latest.get("report_json"), None),
        "diagnosis": _loads_or(latest.get("diagnosis_json"), None),
    }), 200


@app.get("/insights/next-meal")
def insights_next_meal():
    """Today's cached next-meal plates for the app. `status: pending` until the day's
    afternoon run has landed (or if the Mac was offline)."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    today = datetime.now(_tz()).date().isoformat()
    todays = [r for r in _read_cached_rows(NEXT_MEAL_TAB)
              if str(r.get("date") or "") == today]
    if not todays:
        return jsonify({"status": "pending", "plates": []}), 200
    latest = todays[-1]
    return jsonify({
        "status": str(latest.get("status") or "generated"),
        "date": latest.get("date"),
        "generated_at": latest.get("generated_at"),
        "focus_key": latest.get("focus_key") or None,
        "snapshot": _loads_or(latest.get("snapshot_json"), {}),
        "plates": _loads_or(latest.get("plates_json"), []),
    }), 200


# =============================================================================
# Gemini-powered on-demand generation (replaces the local claude CLI).
# These endpoints call Gemini API directly so the feature works without the Mac.
# =============================================================================

# File cache dir for generated reports (Cloud Run has a writable /tmp).
_INSIGHTS_CACHE_DIR = "/tmp/health-tracker-cache"


def _cache_path(name: str) -> str:
    os.makedirs(_INSIGHTS_CACHE_DIR, exist_ok=True)
    return os.path.join(_INSIGHTS_CACHE_DIR, name)


def _write_cache(name: str, data: dict) -> None:
    try:
        with open(_cache_path(name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass  # non-fatal — generation succeeded, caching is best-effort


def _read_cache(name: str) -> dict | None:
    try:
        with open(_cache_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@functools.lru_cache(maxsize=1)
def _narrator_mod():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "narrator.py")
    spec = importlib.util.spec_from_file_location("narrator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gemini_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


_PROFILE_WINDOW_DAYS_NARRATOR = 28


def _get_current_focus() -> Optional[str]:
    """Read the latest focus from the weekly reports sheet tab."""
    rows = [r for r in _read_cached_rows(WEEKLY_TAB)
            if r.get("week_start")]
    if not rows:
        return None
    last = max(rows, key=lambda r: str(r.get("week_start")))
    return str(last.get("focus_key") or "") or None


def _today_key() -> str:
    return datetime.now(_tz()).date().isoformat()


def _today_meal_rows() -> List[Dict[str, Any]]:
    today = _today_key()
    return [r for r in _all_meal_rows()
            if str(r.get("datetime", "")).startswith(today)]


@app.post("/insights/generate-weekly")
def insights_generate_weekly():
    """On-demand weekly report generation via Gemini API.

    Generates the Sunday coaching review with diagnosis + food profile + continuity.
    Uses the Gemini narrator (not local claude) so this works without the Mac.
    Can be called by Cloud Scheduler on Sundays at 09:00.

    Returns the generated report, or errors if Gemini API is not configured.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    if not _gemini_available():
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    now = datetime.now(_tz())
    week_start = (now - timedelta(days=(now.weekday() + 1) % 7)).date().isoformat()

    try:
        diagnosis = _diagnosis_for(week_start, 7)
        profile = _insights_mod().build_food_profile(
            _window_meals(_all_meal_rows(),
                          (datetime.fromisoformat(week_start).date() -
                           timedelta(days=_PROFILE_WINDOW_DAYS_NARRATOR)).isoformat(),
                          week_start),
            NUTRIENT_KEYS, _display_taxonomy())

        if diagnosis.get("window", {}).get("days_logged", 0) < 4:
            return jsonify({
                "status": "skipped",
                "reason": f"only {diagnosis['window']['days_logged']} logged days — "
                          f"too thin for a confident report",
            }), 200

        # Resolve continuity: compare this week's value against last report's focus.
        continuity = None
        prior_rows = [r for r in _read_cached_rows(WEEKLY_TAB) if r.get("week_start")]
        if prior_rows:
            last = max(prior_rows, key=lambda r: str(r.get("week_start")))
            pk = str(last.get("focus_key") or "").strip()
            try:
                prev_val = float(last.get("focus_value"))
            except (TypeError, ValueError):
                prev_val = None
            if pk and prev_val and prev_val > 0:
                # Find current value for this key in the diagnosis nutrients.
                now_val = None
                kind = "reach"
                for n in diagnosis.get("nutrients", []):
                    if n.get("key") == pk:
                        now_val = n.get("mean")
                        kind = n.get("kind", "reach")
                        break
                if not now_val:
                    # Maybe it's in the adherence block (a macro like protein, fiber).
                    adh = diagnosis.get("adherence", {}).get(pk, {})
                    now_val = adh.get("mean") if isinstance(adh, dict) else None

                if now_val and now_val > 0:
                    pct = round(100 * (now_val - prev_val) / prev_val)
                    up = now_val > prev_val
                    toward = (up and kind != "limit") or (not up and kind == "limit")
                    continuity = {
                        "key": pk, "prev": prev_val, "now": now_val,
                        "pct": pct,
                        "direction": "up" if up else "down" if now_val < prev_val else "flat",
                        "toward_target": toward,
                    }

        narrator = _narrator_mod()
        report = narrator.narrate_weekly(diagnosis, profile, continuity)

        # Build the full response and cache it.
        result = {
            "status": "generated",
            "week_start": week_start,
            "generated_at": now.isoformat(timespec="seconds"),
            "window_start": diagnosis.get("window", {}).get("start"),
            "window_end": diagnosis.get("window", {}).get("end"),
            "focus_key": (report.get("focus", {}) or {}).get("key")
                         or (diagnosis.get("ranked_issues") or [""])[0],
            "prior_focus_delta": continuity,
            "coverage_note": diagnosis.get("coverage_note", ""),
            "report": report,
        }
        _write_cache("weekly_report", result)
        return jsonify(result), 200

    except Exception as exc:
        app.logger.exception("weekly generation failed")
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.post("/insights/generate-next-meal")
def insights_generate_next_meal():
    """On-demand next-meal generation via Gemini API.

    Builds the enhanced context (current time, today's meals, meal timing profile,
    remaining budget, shortfall candidates) and calls Gemini to determine the next
    slot AND generate 3 meal options for that slot.

    The AI decides the next slot dynamically based on:
    - Current time of day
    - What meals have been logged today
    - The user's historical meal timing patterns (from 28-day history)
    - Remaining calorie/protein budget
    - Today's shortfall nutrients

    Returns the generated plates, or errors if Gemini is not configured.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    if not _gemini_available():
        return jsonify({"error": "GEMINI_API_KEY not configured"}), 503

    now = datetime.now(_tz())
    today = now.date().isoformat()
    current_time = now.strftime("%H:%M")

    try:
        # Gather data.
        all_rows = _all_meal_rows()
        today_rows = [r for r in all_rows
                      if str(r.get("datetime", "")).startswith(today)]
        consumed = _todays_consumed(today_rows)
        targets, basis = _resolved_targets_and_basis()
        profile_start = (now.date() - timedelta(days=_PROFILE_WINDOW_DAYS_NARRATOR)).isoformat()
        window_meals = _window_meals(all_rows, profile_start, today)
        focus_key = _get_current_focus()

        ins = _insights_mod()
        nm_taxonomy = _display_taxonomy()
        food_profile = ins.build_food_profile(window_meals, NUTRIENT_KEYS,
                                              nm_taxonomy)

        # Build enhanced context with timing profile and today's meals.
        context = ins.next_meal_context_v2(
            consumed=consumed, targets=targets,
            focus_key=focus_key,
            food_profile=food_profile,
            today_rows=today_rows,
            window_meals=window_meals,
            window_days=_PROFILE_WINDOW_DAYS_NARRATOR,
            current_time=current_time, taxonomy=nm_taxonomy,
        )

        if not context.get("candidates"):
            return jsonify({
                "status": "skipped",
                "reason": "nothing short today — no next-meal suggestion needed.",
                "context": context,
            }), 200

        narrator = _narrator_mod()
        result = narrator.assemble_next_meal(context, food_profile)

        # Cache the result.
        cache_entry = {
            "date": today,
            "generated_at": now.isoformat(timespec="seconds"),
            "focus_key": focus_key,
            "next_slot": result.get("next_slot"),
            "reasoning": result.get("reasoning"),
            "plates": result.get("plates", []),
            "snapshot": {
                "current_time": current_time,
                "calories_left": context.get("calories_left"),
                "protein_left_g": context.get("protein_left_g"),
                "today_meals": context.get("today_meals", []),
            },
        }
        _write_cache("next_meal", cache_entry)

        return jsonify({
            "status": "generated",
            "date": today,
            "generated_at": cache_entry["generated_at"],
            "focus_key": focus_key,
            "next_slot": result.get("next_slot"),
            "reasoning": result.get("reasoning"),
            "plates": result.get("plates", []),
        }), 200

    except Exception as exc:
        app.logger.exception("next-meal generation failed")
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.get("/insights/next-meal/v2")
def insights_next_meal_v2():
    """Enhanced next-meal endpoint: returns cached on-demand result, or pending.

    Unlike the sheet-based `/insights/next-meal`, this reads the cache written by
    `POST /insights/generate-next-meal`. The iOS app should call this after
    generating, or call `POST /insights/generate-next-meal` directly.

    Returns:
      - Cached result if available for today (status: "generated")
      - Or status "pending" if no cached result exists yet
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    today = _today_key()
    cached = _read_cache("next_meal")
    if cached and cached.get("date") == today:
        return jsonify({
            "status": "generated",
            "date": cached.get("date"),
            "generated_at": cached.get("generated_at"),
            "focus_key": cached.get("focus_key"),
            "next_slot": cached.get("next_slot"),
            "reasoning": cached.get("reasoning"),
            "plates": cached.get("plates", []),
        }), 200
    return jsonify({"status": "pending", "plates": []}), 200



# =============================================================================
# The Coach (v2): a background-generated card feed, chat, and long-term memory.
#
# Everything the app reads on this path is a Cloud Storage read of already-written
# JSON — no model call, no Sheets round trip on the critical path. That is the whole
# design, and it is a direct answer to how the first version behaved: it cached
# generated cards in /tmp on a service that scales to zero, so the cache was empty
# on almost every open and the app fell through to a 45-second Gemini call on the
# screen the user was looking at. Worse, when nothing was nutritionally short that
# day the generator returned "skipped" with no plates, and the app's sheet sat on
# "preparing…" forever with nothing that could ever fill it.
#
# So:
#   GET  /coach/feed        pure read. Always answers, in ~100 ms, with whatever
#                           exists plus `stale` and `generating` so the app can
#                           decide between rendering and quietly refreshing.
#   POST /coach/refresh     202 + a Cloud Tasks enqueue. Never blocks a request on
#                           a model.
#   POST /coach/generate    the worker. Called by Cloud Scheduler (four slots a
#                           day), by the queue, or by hand.
#   POST /coach/chat        a conversation anchored to one card, which also folds
#                           anything durable it learns into memory.
#   GET  /coach/thread/<id> one conversation.
#   GET/POST/DELETE /coach/memory   read, add to, and correct what the coach
#                           remembers — a memory the user can't edit is one they
#                           can't correct.
#
# The legacy /insights/* endpoints above are left untouched: the installed app build
# still reads them, and the backend deploys long before a new build lands on the
# phone.
# =============================================================================

# The food vocabulary window. Four weeks is long enough for "no fish in a fortnight"
# to mean something and short enough to describe how the user eats *now*.
COACH_WINDOW_DAYS = 28

# How far back memory retrieval looks. Ninety days is enough to recall "the last time
# this happened" for anything seasonal without the read costing more than a few blobs
# — the archive is sharded by month, so this is three reads at worst. Older material
# reaches the model through the weekly and monthly reports instead, which is the point
# of consolidating them.
COACH_RECALL_DAYS = 90

# A feed older than this asks the app to kick a background refresh when it opens.
# Four hours is shorter than the gap between the scheduled slots on purpose: opening
# the app at 13:00 should quietly refresh into something about lunch rather than still
# be talking about breakfast.
COACH_STALE_HOURS = 4.0


def _coach_mod(name: str):
    """Import one of the coach modules that sit next to main.py.

    The insights core is loaded by explicit file path (see `_insights_mod`) because
    it predates having several of these; the coach modules import each other by
    name, so what they need is the directory on `sys.path` — which is already true
    in the container (everything is flattened into /app) and is made true here for
    a file-path import in the tests.
    """
    import importlib
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    return importlib.import_module(name)


# Sized to hold every coach module at once. It used to be 8, which was already one
# short of the modules in play — and an evicted entry means re-importing on the next
# call for no reason.
@functools.lru_cache(maxsize=24)
def _coach(name: str):
    return _coach_mod(name)


def _gemini_call(require_key: str, *, temperature: float = 0.3):
    """A `prompt -> parsed JSON` callable for the pure modules to inject. Keeps the
    model choice (and the API key) here, and keeps `food_taxonomy` / `coach_feed`
    testable with a fake."""
    narrator = _narrator_mod()

    def call(prompt: str) -> Dict[str, Any]:
        return narrator.call_gemini(prompt, require_key=require_key,
                                    temperature=temperature)
    return call


def _coach_window_meals(now: datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(window meals, today's meals) from ONE read of the meals tab."""
    all_rows = _all_meal_rows()
    today = now.date().isoformat()
    start = (now.date() - timedelta(days=COACH_WINDOW_DAYS)).isoformat()
    return _window_meals(all_rows, start, today), [
        r for r in all_rows if str(r.get("datetime", "")).startswith(today)]


# How far back the non-food domains look. Longer than the food window on purpose:
# a body-composition trend needs weeks to mean anything, and the correlation engine
# needs enough paired days to clear its own minimum n — 28 days of food patterns is
# plenty for "you haven't eaten fish in twelve days" but far too short to say
# anything honest about late dinners and deep sleep.
COACH_METRIC_WINDOW_DAYS = int(os.environ.get("COACH_METRIC_WINDOW_DAYS", "120"))

# The waking-day cutoff, mirroring src/run_daily.DAY_CUTOFF_HOUR. Both images read
# the same env var, so the two copies cannot be configured apart.
NUTRITION_DAY_CUTOFF_HOUR = int(os.environ.get("NUTRITION_DAY_CUTOFF_HOUR", "5"))


def _coach_window_days(now: datetime,
                       window_days: int = COACH_METRIC_WINDOW_DAYS
                       ) -> List[Dict[str, Any]]:
    """The `daily_summary` rows the coach reasons over, oldest first.

    Until now the coach could see only the meals tab, which is why it could talk
    about food and nothing else. Everything the tracker and the scale measure enters
    here — and it is one read, shared by the domain findings and the link engine.
    """
    try:
        rows = _rows_as_dicts(_read_tab(DAILY_TAB))
    except Exception:
        app.logger.warning("daily_summary read failed for the coach", exc_info=True)
        return []
    start = (now.date() - timedelta(days=window_days)).isoformat()
    today = now.date().isoformat()
    days = [r for r in rows if start <= str(r.get("date", "")) <= today]
    days.sort(key=lambda r: str(r.get("date", "")))
    return days


def _waking_day(stamp: Any) -> str:
    """The waking day a meal belongs to (05:00 cutoff), mirroring
    `src.run_daily.nutrition_day` — a 00:17 dessert counts toward the evening it
    followed. Mirrored rather than imported because ingest cannot import `src`; a
    test pins the two implementations against each other."""
    text = str(stamp or "")
    if len(text) < 16:
        return text[:10]
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T")[:19])
    except ValueError:
        return text[:10]
    return (parsed - timedelta(hours=NUTRITION_DAY_CUTOFF_HOUR)).date().isoformat()


def _meals_by_waking_day(meal_rows: Sequence[Dict[str, Any]]
                         ) -> Dict[str, List[Dict[str, Any]]]:
    """Meals bucketed the way the nutrition columns are keyed, so a meal-derived
    feature and the `daily_summary` row it is compared against describe the same
    day. Stubs are dropped — an `analysis failed` row is not a meal."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in meal_rows:
        if _is_stub(row):
            continue
        day = _waking_day(row.get("datetime"))
        if day:
            out.setdefault(day, []).append(row)
    return out


# The handful of numbers a card may cite directly, per domain: the latest reading and
# the person's own recent average, which is the only thing that makes a reading mean
# anything. Deliberately NOT the raw window — the findings are the analysis, and
# handing the model 80 columns as well would invite it to do its own and get it wrong.
_METRIC_SUMMARY: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "sleep": (("sleep_mins", "sono (min)"),
              ("sleep_efficiency_pct", "eficiência (%)"),
              ("sleep_deep_mins", "sono profundo (min)"),
              ("resting_hr_bpm", "FC repouso (bpm)"),
              ("hrv_ms", "HRV (ms)")),
    "activity": (("steps", "passos"),
                 ("workout_mins", "treino (min)"),
                 ("total_cals_out", "gasto total (kcal)")),
    "body": (("weight_kg", "peso (kg)"),
             ("body_fat_pct", "massa gorda (%)"),
             ("lean_mass_kg", "massa magra (kg)")),
}
# How many recent days the "usual" figure averages over.
_METRIC_RECENT_DAYS = 14


def _coach_metrics_summary(days: Sequence[Dict[str, Any]],
                           caps: "caps_mod.Capabilities"
                           ) -> Optional[Dict[str, Any]]:
    """Latest reading + recent average per headline metric, for the domains this user
    has. None when there is nothing to say, so the key drops out of the prompt
    entirely rather than sitting there as an empty object inviting comment."""
    if not days:
        return None
    out: Dict[str, Any] = {}
    for domain, metrics in _METRIC_SUMMARY.items():
        if domain not in caps.domains():
            continue
        block: Dict[str, Any] = {}
        for name, label in metrics:
            values = [v for v in (_num_or_none(d.get(name))
                                  for d in days[-_METRIC_RECENT_DAYS:])
                      if v is not None]
            if not values:
                continue
            block[name] = {
                "label_pt": label,
                "latest": round(values[-1], 1),
                "usual": round(sum(values) / len(values), 1),
                "days": len(values),
            }
        if block:
            out[domain] = block
    return out or None


def _num_or_none(value: Any) -> Optional[float]:
    """A sheet cell as a float. Booleans are rejected: a TRUE flag silently becoming
    1.0 inside an average is how confident nonsense gets made."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _coach_metric_findings(days: Sequence[Dict[str, Any]],
                           caps: "caps_mod.Capabilities",
                           meals_by_day: Dict[str, List[Dict[str, Any]]]
                           ) -> List[Dict[str, Any]]:
    """The deterministic non-food findings plus the cross-domain links.

    Both halves are capability-gated at the source: `domains` decides which rules run
    at all, and a link is skipped unless the user measures every block it spans. A
    nutrition-only user therefore reaches the model with exactly the payload the old
    coach had, which is what makes adding a friend a config change rather than a
    code path.
    """
    if not days:
        return []
    findings_mod = _coach("domain_findings")
    links_mod = _coach("links")

    domains = [d for d in caps.domains() if d != "nutrition"]
    out: List[Dict[str, Any]] = list(
        findings_mod.build_findings(days, domains=domains))

    try:
        out.extend(links_mod.evaluate(
            days,
            features_by_day=links_mod.daily_features(meals_by_day),
            columns={c.name: c.causal for c in DAILY_COLUMNS},
            blocks=caps.blocks))
    except Exception:
        # A correlation is a bonus, never a reason to lose the whole feed.
        app.logger.warning("link evaluation failed (non-fatal)", exc_info=True)

    out.sort(key=lambda f: -f.get("severity", 0))
    return out


def _coach_profile(now: datetime, window_meals: List[Dict[str, Any]], *,
                   learn: bool = False) -> Dict[str, Any]:
    """The deterministic food-level reading of the window.

    `learn=True` first asks the model to classify any food the curated taxonomy
    can't place, and persists what it learns — done on the scheduled runs only, so a
    new food costs one small call once and every read afterwards is free.
    """
    store = _coach("coach_store")
    patterns = _coach("food_patterns")
    taxonomy_mod = _coach("food_taxonomy")

    taxonomy = store.read_json(store.TAXONOMY, default=taxonomy_mod.empty_taxonomy())
    if learn and _gemini_available():
        names = [str(i.get("name") or "")
                 for row in window_meals
                 for i in patterns._parse_items(row.get("items"))]
        taxonomy, learned = taxonomy_mod.classify_unknown(
            taxonomy, names, _gemini_call("foods", temperature=0.1))
        # The pt-PT half of the same reference table. Meals logged since the ingest
        # model started writing `name_pt` never reach this, so what it sees is the
        # historical backlog plus the odd food the curated lexicon doesn't carry —
        # a set that shrinks to nothing. Learned here, on the background generation
        # path, so no request ever waits on a translation.
        taxonomy, translated = taxonomy_mod.translate_unknown(
            taxonomy, names, _gemini_call("foods", temperature=0.0))
        if learned or translated:
            store.write_json(store.TAXONOMY, taxonomy)
            app.logger.info("taxonomy learned %d new foods, %d new pt names",
                            learned, translated)

    return patterns.build_food_profile(
        window_meals, taxonomy=taxonomy, window_days=COACH_WINDOW_DAYS,
        ref_day=now.date().isoformat())


# The micronutrients worth putting in front of the model per meal. The full set of
# 37 would bury the food under numbers; these are the ones a dietitian would actually
# comment on when reading a plate.
_COACH_MEAL_NUTRIENTS = ("fiber_g", "saturated_fat_g", "sugar_g", "sodium_mg",
                         "omega3_g", "iron_mg", "calcium_mg", "vitamin_c_mg")


def _coach_today(now: datetime, today_rows: List[Dict[str, Any]],
                 consumed: Dict[str, float],
                 targets: Dict[str, Dict[str, Any]],
                 taxonomy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Today so far, in full detail: every meal, every item in it, with grams, macros
    and the micronutrients worth commenting on.

    The first version handed the model a one-line summary per meal ("aveia, banana,
    leite — 420 kcal"), which is exactly enough to produce "protein is on track, have
    more protein at dinner" and nothing better. Judging a *choice* — why the oats held
    the morning, whether lunch was balanced — needs the actual plate.
    """
    patterns = _coach("food_patterns")
    cal_t = targets.get("calories", {})
    cal_left = max(0.0, (cal_t.get("ceiling") or cal_t.get("floor") or 0)
                   - _round_num(consumed.get("calories")))
    prot_t = targets.get("protein_g", {})
    prot_left = max(0.0, (prot_t.get("floor") or 0)
                    - _round_num(consumed.get("protein_g")))

    meals: List[Dict[str, Any]] = []
    for meal in patterns.read_meals(today_rows, taxonomy):
        items = []
        for item in meal["items"]:
            # pt-PT, and the logged-detail one rather than the canonical bucket:
            # this is the model's view of one specific plate, so "peito de frango
            # grelhado" is exactly the detail it should be commenting on.
            entry = {"food": item.get("pt") or item["food"],
                     "grams": round(item["grams"]),
                     "group": item["group"], "calories": round(item["calories"])}
            if item["fried"]:
                entry["fried"] = True
            micros = {k: round(v, 1) for k, v in item["nutrients"].items()
                      if k in _COACH_MEAL_NUTRIENTS and _round_num(v) > 0}
            if micros:
                entry["nutrients"] = micros
            items.append(entry)
        entry_meal = {
            "time": meal["datetime"][11:16],
            "slot": patterns.SLOT_LABELS.get(meal["slot"], meal["slot"]),
            "calories": round(meal["calories"]),
            "protein_g": round(meal["protein_g"], 1),
            "food_groups": meal["groups"],
            "items": items,
        }
        # The user's own words, when there are any. This is the difference between
        # "hambúrguer, batatas fritas e chá gelado" and "comi um menu médio Big Tasty
        # do McDonalds" — the second is context the user supplied and the coach spent
        # its first week never seeing.
        if meal.get("note"):
            entry_meal["your_note"] = meal["note"]
        meals.append(entry_meal)

    return {
        "date": now.date().isoformat(),
        "current_time": now.strftime("%H:%M"),
        "meals": meals,
        "meals_logged": len(meals),
        "calories_eaten": round(_round_num(consumed.get("calories"))),
        "calories_left": round(cal_left),
        "protein_eaten_g": round(_round_num(consumed.get("protein_g"))),
        "protein_left_g": round(prot_left),
        "totals": {key: round(_round_num(consumed.get(key)), 1)
                   for key in ("calories", "protein_g", "carbs_g", "fat_g",
                               "fiber_g", "saturated_fat_g", "sugar_g")
                   if _round_num(consumed.get(key)) > 0},
    }


def _coach_nutrients(consumed: Dict[str, float],
                     targets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The nutrient picture, deliberately compressed to a handful of ratios.

    It is *supporting* evidence now, not the subject: the whole content problem with
    the first coach was that this was the only thing it could see, so it could only
    ever tell the user what the Nutrients tab already showed.
    """
    out: Dict[str, Any] = {}
    for key in ("protein_g", "fiber_g", "calories"):
        target = targets.get(key) or {}
        floor = target.get("floor") or target.get("ceiling")
        if not floor:
            continue
        out[key] = {"eaten": round(_round_num(consumed.get(key)), 1),
                    "target": floor,
                    "pct": round(_round_num(consumed.get(key)) / floor, 2)}
    return out


def _next_meal_context(now: datetime, *, profile: Dict[str, Any],
                       today: Dict[str, Any], consumed: Dict[str, float],
                       targets: Dict[str, Dict[str, Any]],
                       window_meals: List[Dict[str, Any]],
                       memory: Dict[str, Any],
                       taxonomy: Optional[Dict[str, Any]] = None,
                       ) -> Dict[str, Any]:
    """Everything the plate generator gets: the clock, the day, the user's timing
    habits, the remaining budget, and candidate foods from BOTH the nutrient
    shortfalls and the food-level findings."""
    ins = _insights_mod()
    feed = _coach("coach_feed")
    memory_mod = _coach("coach_memory")

    # The taxonomy travels down so the nutrient-side candidates name their foods
    # in the same pt-PT as the food-pattern side; two halves of one prompt calling
    # the same food two different things is worse than either name alone.
    legacy_profile = ins.build_food_profile(window_meals, NUTRIENT_KEYS, taxonomy)
    nutrient_ctx = ins.next_meal_context_v2(
        consumed=consumed, targets=targets, focus_key=None,
        food_profile=legacy_profile, today_rows=[], window_meals=window_meals,
        window_days=COACH_WINDOW_DAYS, current_time=now.strftime("%H:%M"),
        taxonomy=taxonomy)
    slot_hint = feed.slot_for(now)
    return {
        "current_time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "today_meals": today.get("meals", []),
        "meal_pattern": ins.build_meal_timing_profile(window_meals,
                                                      COACH_WINDOW_DAYS),
        "calories_left": today.get("calories_left"),
        "protein_left_g": today.get("protein_left_g"),
        "shortfalls_today": nutrient_ctx.get("shortfalls_today", []),
        "candidates": feed.next_meal_candidates(
            profile, nutrient_candidates=nutrient_ctx.get("candidates", {}),
            slot_hint=slot_hint),
        "memory": memory_mod.for_prompt(memory, limit=12),
    }


def _read_feed_cards(now: datetime) -> List[Dict[str, Any]]:
    """Every card that is still valid, in feed order — one storage read.

    Cards carry their own expiry, so last night's summary is still here at 00:05 and
    the Sunday review is still here on Thursday, without the reader having to know how
    long any card lives.
    """
    store = _coach("coach_store")
    feed = _coach("coach_feed")
    payload = store.read_json(store.FEED, default=None)
    cards = (payload.get("cards") or []) if isinstance(payload, dict) else []
    return feed.live_cards([c for c in cards if isinstance(c, dict)], now=now)


@app.get("/coach/feed")
def coach_feed_read():
    """The app's only read. Pure storage — never calls a model, never touches the
    spreadsheet, so it answers in ~100 ms whether or not anything has been generated
    yet.

    `generating` is true while a background run is in flight, which is what lets the
    app show a real progress indicator instead of an empty screen; `stale` says the
    newest card is old enough to be worth refreshing.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    feed = _coach("coach_feed")
    now = datetime.now(_tz())
    cards = _read_feed_cards(now)
    state = store.read_state()
    queue = store.queue_summary(now.isoformat())
    return jsonify({
        "status": "ready" if cards else "empty",
        "generated_at": max((str(c.get("created_at") or "") for c in cards),
                            default=None),
        "server_time": now.isoformat(timespec="seconds"),
        # Stale by age OR by context: cards written this morning can be perfectly
        # fresh and still have nothing to say about the evening the user just opened
        # the app in.
        "stale": (feed.is_stale(cards, now=now, max_age_hours=COACH_STALE_HOURS)
                  or feed.context_stale(cards, now=now)),
        # Only a job a worker is actually running counts as "generating". A job
        # merely waiting for a sleeping laptop must not put a progress bar on screen
        # for hours — that is the old "loading forever" bug in a new costume.
        "generating": (store.job_is_live(state, now.isoformat())
                       or queue["running"]),
        "queued": queue["waiting"],
        "cards": cards,
    }), 200


@app.post("/coach/refresh")
def coach_refresh():
    """Ask for fresh cards and return immediately (202).

    The work goes to the Cloud Tasks queue that already carries meal analysis, so
    the app never waits on Gemini and a transient failure is retried by the queue
    rather than surfaced as a broken screen. If the queue is unreachable the reply
    says so honestly (`queued: false`) and the app keeps showing what it has.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    feed = _coach("coach_feed")
    now = datetime.now(_tz())
    slot = str(body.get("slot") or "adhoc")
    if slot not in feed.SLOTS:
        slot = "adhoc"
    reason = str(body.get("reason") or "manual")[:40]

    try:
        _enqueue_coach_generate(slot, reason)
        return jsonify({"queued": True, "slot": slot,
                        "server_time": now.isoformat(timespec="seconds")}), 202
    except Exception:
        app.logger.exception("coach refresh enqueue failed")
        return jsonify({"queued": False, "slot": slot,
                        "error": "queue unavailable"}), 202


def _enqueue_coach_generate(slot: str, reason: str, *,
                            delay_s: int = 0,
                            not_before_meal: str = "") -> None:
    """Hand a generation to the Cloud Tasks queue that already runs meal analysis.

    `delay_s` schedules it into the future, and `not_before_meal` carries the
    "nothing has been logged since" condition that makes the delay a *debounce* —
    see `_trigger_coach_refresh`.
    """
    url = os.environ.get("COACH_GENERATE_URL") or (
        os.environ["PROCESS_URL"].rsplit("/", 1)[0] + "/coach/generate")
    payload: Dict[str, Any] = {"slot": slot, "reason": reason}
    if not_before_meal:
        payload["only_if_no_meal_since"] = not_before_meal

    if _queue_backend() == "local":
        import localqueue
        # `delay_s` is the debounce, so it must survive the switch: the local
        # queue's `not_before` is the same idea as Cloud Tasks' schedule_time.
        localqueue.enqueue(
            url, json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json",
             "X-Auth-Token": os.environ.get("INGEST_TOKEN", "")},
            delay_s=float(delay_s))
        return

    from google.cloud import tasks_v2  # lazy: keeps tests importable without the lib
    from google.protobuf import timestamp_pb2
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(
        os.environ["GCP_PROJECT"],
        os.environ.get("TASKS_LOCATION", "europe-west1"),
        os.environ["TASKS_QUEUE"],
    )
    task: Dict[str, Any] = {"http_request": {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": url,
        "headers": {"Content-Type": "application/json",
                    "X-Auth-Token": os.environ.get("INGEST_TOKEN", "")},
        "body": json.dumps(payload).encode("utf-8"),
    }}
    if delay_s > 0:
        stamp = timestamp_pb2.Timestamp()
        stamp.FromDatetime(datetime.now(_tz()) + timedelta(seconds=delay_s))
        task["schedule_time"] = stamp
    client.create_task(parent=parent, task=task)


# How long the coach waits after a logged meal before it reads the day. A meal is
# rarely one entry — a plate, then the drink, then the fruit ten minutes later — and
# analysing after the first one would describe half a meal. An hour of quiet is a
# good proxy for "that meal is over".
COACH_QUIET_MINUTES = int(os.environ.get("COACH_QUIET_MINUTES", "60"))

# How long an unanswered job suppresses an identical one. Long enough that a laptop
# asleep all morning collects one job per part of the day rather than one per app
# open, short enough that a genuinely new situation still gets asked about.
COACH_JOB_DEDUP_MINUTES = int(os.environ.get("COACH_JOB_DEDUP_MINUTES", "45"))


def _trigger_coach_refresh(reason: str) -> None:
    """Debounce a generation an hour past the last logged meal.

    Each meal records itself and schedules a run `COACH_QUIET_MINUTES` out carrying
    the timestamp it was scheduled for. When that run fires it regenerates only if
    nothing has been logged since — so a second helping forty minutes later doesn't
    produce a second analysis, it postpones the first. The task that eventually finds
    a quiet hour behind it is the one that speaks.

    Never raises and never blocks: the meal is already saved, and a late card is a far
    smaller problem than a failed log.
    """
    store = _coach("coach_store")
    stamp = datetime.now(_tz()).isoformat(timespec="seconds")
    try:
        store.update_state(lambda state: {**state, "last_meal_at": stamp})
    except Exception:
        app.logger.info("could not record last_meal_at", exc_info=True)
    try:
        _enqueue_coach_generate("adhoc", reason,
                                delay_s=COACH_QUIET_MINUTES * 60,
                                not_before_meal=stamp)
    except Exception:
        app.logger.info("coach refresh not enqueued (%s)", reason, exc_info=True)


@app.post("/coach/generate")
def coach_generate():
    """Prepare a generation: compute the facts, build the prompt, park it as a job.

    This endpoint does NOT call a model. It writes a job that the Mac worker claims
    and answers with Sonnet (`GET /coach/work` + `POST /coach/work/<id>`); if nobody
    claims it within `COACH_SONNET_WAIT_HOURS`, `/coach/sweep` runs the same prompt
    through Gemini. Quality is the reason: the local subscription model is worth
    waiting hours for, and the fallback exists so waiting can never mean silence.

    Called by the Cloud Tasks debounce an hour after the last logged meal, by Cloud
    Scheduler for the morning and weekly slots, or by hand. Idempotent: cards carry a
    deterministic id per (day, kind, topic), so a retry replaces its own cards.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    store = _coach("coach_store")
    feed = _coach("coach_feed")

    body = request.get_json(silent=True) or {}
    now = datetime.now(_tz())
    slot = str(body.get("slot") or "").strip() or feed.slot_for(now)
    if slot not in feed.SLOTS:
        return jsonify({"error": f"unknown slot {slot!r}",
                        "known": list(feed.SLOTS)}), 400
    reason = str(body.get("reason") or "schedule")[:40]

    # The debounce. A task scheduled an hour after a meal carries that meal's
    # timestamp; if something has been logged since, the meal is still going and a
    # later task will cover it.
    only_if_no_meal_since = str(body.get("only_if_no_meal_since") or "")
    if only_if_no_meal_since:
        last_meal = str(store.read_state().get("last_meal_at") or "")
        if last_meal and last_meal > only_if_no_meal_since:
            app.logger.info("coach: still eating (last meal %s), skipping", last_meal)
            return jsonify({"status": "superseded", "slot": slot,
                            "last_meal_at": last_meal}), 200

    # One job per question. The app asks for a refresh whenever the feed has nothing
    # to say about the current part of the day, so without this every open while the
    # Mac sleeps would queue another identical job for it to chew through later.
    for existing_job in store.list_jobs():
        if existing_job.get("done_at") or existing_job.get("slot") != slot:
            continue
        age = store._age_seconds(existing_job.get("created_at"),
                                 now.isoformat(timespec="seconds"))
        if age is not None and age < COACH_JOB_DEDUP_MINUTES * 60:
            return jsonify({"status": "already-queued", "job": existing_job["id"],
                            "slot": slot}), 200

    try:
        job = _build_generation_job(now, slot=slot, reason=reason)
    except Exception as exc:
        app.logger.exception("coach job preparation failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    if job is None:
        return jsonify({"status": "empty", "slot": slot,
                        "reason": "nothing worth generating"}), 200

    store.write_json(store.job_path(job["id"]), job)
    app.logger.info("coach job %s queued (slot=%s, %d chars)", job["id"], slot,
                    len(job["prompt"]))

    # Nudge the Mac if it is awake and listening; harmless if it isn't.
    return jsonify({"status": "queued", "job": job["id"], "slot": slot,
                    "waiting_for": "sonnet"}), 202


def _coach_memory_context(now: datetime, *, profile: Dict[str, Any],
                          today: Dict[str, Any],
                          events: Sequence[Dict[str, Any]],
                          memory: Dict[str, Any],
                          findings: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The bounded slice of history this generation should see.

    Never the whole archive. `coach_recall` ranks by relevance to what happened today,
    then recency, then importance, and clips each section to a token budget — so a day
    with drinks in it recalls previous drinking and what was advised then, and a
    nothing-special Tuesday recalls almost nothing and costs almost nothing.
    """
    archive = _coach("coach_archive")
    recall = _coach("coach_recall")
    memory_mod = _coach("coach_memory")

    day_iso = now.date().isoformat()
    lookback = (now.date() - timedelta(days=COACH_RECALL_DAYS)).isoformat()
    topics = recall.query_topics_for(profile=profile, today=today, events=events,
                                     findings=findings)
    entries = archive.read_range(lookback, day_iso)
    recent_cards = [e for e in entries if e.get("kind") == "card"][-8:]
    reports = (archive.recent_reports("weekly", before="9999", limit=1)
               + archive.recent_reports("monthly", before="9999", limit=1))

    return recall.assemble(
        today_iso=day_iso, profile_facts=memory_mod.for_prompt(memory),
        recent_cards=recent_cards, archive=entries, events=events,
        reports=reports, query_topics=topics)


def _build_generation_job(now: datetime, *, slot: str,
                          reason: str) -> Optional[Dict[str, Any]]:
    """The deterministic half of a generation: every fact, and the prompt built from
    it. Whichever model answers, it answers THIS.

    Returns None when there is nothing worth saying — a log too thin for any finding
    and no meals today is not a coach problem, it is an empty diary.
    """
    store = _coach("coach_store")
    feed = _coach("coach_feed")
    memory_mod = _coach("coach_memory")
    narrator = _narrator_mod()

    caps = _capabilities()
    window_meals, today_rows = _coach_window_meals(now)
    consumed = _todays_consumed(today_rows)
    targets, _basis = _resolved_targets_and_basis()
    # Everything the tracker and the scale measured. Read once, used twice: the
    # per-domain findings and the link engine share this window.
    metric_days = _coach_window_days(now) if caps.domains() != ("nutrition",) else []
    metric_findings = _coach_metric_findings(
        metric_days, caps, _meals_by_waking_day(window_meals))
    # Scheduled runs pay for taxonomy learning; the meal-triggered ones read it as-is.
    profile = _coach_profile(now, window_meals, learn=slot != "adhoc")
    taxonomy = store.read_json(store.TAXONOMY, default=None)
    memory = store.read_json(store.MEMORY, default=memory_mod.empty())
    today = _coach_today(now, today_rows, consumed, targets, taxonomy=taxonomy)
    nutrients = _coach_nutrients(consumed, targets)
    state = store.read_state()
    existing = _read_feed_cards(now)

    if not profile.get("findings") and not today.get("meals") \
            and not metric_findings:
        return None

    # What HAPPENED today, as opposed to how this person eats in general — drinks,
    # a meal eaten out, a day well over the ceiling, and the notes the user wrote.
    events_mod = _coach("coach_events")
    patterns = _coach("food_patterns")
    day_iso = now.date().isoformat()
    all_meals = patterns.read_meals(window_meals, taxonomy)
    events = events_mod.detect(
        all_meals, day=day_iso,
        notes=events_mod.notes_for(all_meals, today_rows),
        calories=_round_num(consumed.get("calories")),
        calorie_ceiling=float((targets.get("calories") or {}).get("ceiling") or 0))
    if events:
        _coach("coach_archive").record_events(events)

    facts = feed.build_generation_facts(
        slot=slot, now=now, profile=profile, today=today, nutrients=nutrients,
        memory=memory, state=state,
        next_meal=(_next_meal_context(now, profile=profile, today=today,
                                      consumed=consumed, targets=targets,
                                      window_meals=window_meals, memory=memory,
                                      taxonomy=taxonomy)
                   if "next_meal" in feed._wants(slot) else None),
        weekly=_coach_weekly_facts(profile) if slot == "weekly" else None,
        metric_findings=metric_findings,
        metrics=_coach_metrics_summary(metric_days, caps),
        capabilities=caps.to_api(),
        recent=[{"title": str(c.get("title") or ""),
                 "body": str(c.get("body") or "")} for c in existing])

    # The memory half of the prompt: bounded, ranked, and about today specifically
    # rather than a dump of history. See coach_recall for why it is built this way.
    facts["memory"] = _coach_memory_context(
        now, profile=profile, today=today, events=events, memory=memory,
        findings=list(facts.get("_findings_index", {}).values()))
    facts["today_events"] = events_mod.for_prompt(events)

    # The findings index travels with the job rather than inside the prompt: the
    # answer has to be validated against the same objects that produced the question,
    # and re-deriving them at validation time could silently drift.
    findings = list(facts.pop("_findings_index", {}).values())

    return {
        "id": f"{now.strftime('%Y%m%dT%H%M%S')}-{slot}",
        "slot": slot,
        "reason": reason,
        "created_at": now.isoformat(timespec="seconds"),
        "claimed_at": None,
        "claimed_by": None,
        "attempts": 0,
        "prompt": narrator.build_feed_prompt(facts),
        "require_key": "cards",
        # Everything assembly needs, so answering a job never re-reads the sheet.
        "context": {
            "profile": {"foods": profile.get("foods", []),
                        "swaps": profile.get("swaps", {}),
                        "days_logged": profile.get("days_logged")},
            "today": {"calories_left": today.get("calories_left"),
                      "protein_left_g": today.get("protein_left_g"),
                      "meals": today.get("meals", [])},
            # Carries the non-food findings too: assembly validates the answer
            # against the same objects that produced the question, and a `link`
            # card is dropped unless its ref is in here.
            "findings": findings,
        },
    }


@app.get("/coach/work")
def coach_work():
    """The Mac worker asks for something to do.

    Returns one job with its prompt, or 204 when the queue is empty. The claim is a
    lease: a laptop that sleeps mid-run releases the job back after 15 minutes rather
    than stranding it.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    now = datetime.now(_tz())
    worker = str(request.args.get("worker") or "mac")[:40]
    job = store.claim_next_job(worker, now.isoformat(timespec="seconds"))
    if job is None:
        return "", 204
    return jsonify({"id": job["id"], "slot": job["slot"],
                    "prompt": job["prompt"],
                    "require_key": job.get("require_key", "cards"),
                    # Reports ask for the strong model at a slower setting; the daily
                    # feed does not. The job carries its own answer to that.
                    "model": job.get("model"),
                    "effort": job.get("effort"),
                    # A chat turn names a model but is not a report, so it must not
                    # inherit the report's 15-minute budget.
                    "timeout_s": job.get("timeout_s"),
                    "created_at": job.get("created_at"),
                    "attempts": job.get("attempts")}), 200


@app.post("/coach/work/<job_id>")
def coach_work_result(job_id: str):
    """The Mac worker hands back what Sonnet said — or admits it couldn't.

    `{"answer": {...}}` completes the job. `{"release": "reason"}` puts it back on
    the queue untouched, which is what an exhausted usage window does: the job stays
    pending, the worker tries again later, and if the wait runs past
    `COACH_SONNET_WAIT_HOURS` the sweeper falls back to Gemini.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    if not store.is_safe_id(job_id):
        return jsonify({"error": "bad job id"}), 400
    job = store.read_json(store.job_path(job_id), default=None)
    if not isinstance(job, dict):
        return jsonify({"error": "no such job"}), 404

    body = request.get_json(silent=True) or {}
    now = datetime.now(_tz())
    if body.get("release"):
        store.release_job(job_id, str(body["release"]), now.isoformat(timespec="seconds"))
        app.logger.info("coach job %s released: %s", job_id, body["release"])
        return jsonify({"status": "released", "job": job_id}), 200

    answer = body.get("answer")
    if not isinstance(answer, dict):
        return jsonify({"error": "answer must be an object"}), 400

    source = str(body.get("model") or "sonnet")[:60]
    if job.get("chat"):
        written = _apply_chat(job, answer, now, source=source)
    elif job.get("report"):
        written = _apply_report(job, answer, now, source=source)
    else:
        written = _apply_generation(job, answer, now, source=source)
    store.finish_generation_job(job_id)
    return jsonify({"status": "applied", "job": job_id, **written}), 200


def _apply_generation(job: Dict[str, Any], answer: Dict[str, Any], now: datetime,
                      *, source: str) -> Dict[str, Any]:
    """Validate one model answer and fold the resulting cards into the feed.

    Both models land here. Sonnet gets no more benefit of the doubt than Gemini: the
    swap validation, the finding checks and the id/expiry re-attachment are the same
    code either way.
    """
    store = _coach("coach_store")
    feed = _coach("coach_feed")
    context = job.get("context") or {}
    slot = job.get("slot", "adhoc")

    cards, shown = feed.assemble(
        answer, slot=slot, now=now,
        profile=context.get("profile") or {},
        today=context.get("today") or {},
        findings=context.get("findings") or [])
    if not cards:
        return {"cards": 0, "source": source}

    for card in cards:
        card["source"] = source

    stamp = now.isoformat(timespec="seconds")
    merged_holder: Dict[str, Any] = {}

    def merge_into(current: Any) -> Dict[str, Any]:
        existing = (current.get("cards") or []) if isinstance(current, dict) else []
        merged_holder["cards"] = feed.merge_cards(existing, cards, now=now)
        return {"generated_at": stamp, "slot": slot, "source": source,
                "cards": merged_holder["cards"]}
    store.update_json(store.FEED, merge_into, default={"cards": []})

    # The feed forgets on purpose; the archive does not. Everything the coach says is
    # kept so the weekly review can read what was actually advised, and so a report a
    # year from now has something to read at all.
    try:
        _coach("coach_archive").record_cards(cards, now=now)
    except Exception:
        app.logger.exception("archiving cards failed (non-fatal)")

    def note_run(current: Dict[str, Any]) -> Dict[str, Any]:
        current.setdefault("runs", {})[slot] = stamp
        current.setdefault("shown", {}).update(shown)
        return current
    store.update_state(note_run)

    merged = merged_holder.get("cards", cards)
    app.logger.info("coach %s via %s: %d cards (%d in feed)", slot, source,
                    len(cards), len(merged))
    return {"cards": len(cards), "feed": len(merged), "source": source,
            "findings_shown": list(shown)}


# How long a job may wait for Sonnet before Gemini takes it. Five hours is Claude's
# usage window: if the whole window passes without the Mac being awake and inside its
# allowance, waiting longer is unlikely to help and the user has been without a
# coach all day.
COACH_SONNET_WAIT_HOURS = float(os.environ.get("COACH_SONNET_WAIT_HOURS", "5"))


@app.post("/coach/sweep")
def coach_sweep():
    """The fallback: run anything that has waited too long through Gemini.

    Cloud Scheduler calls this periodically. It is the only place Gemini generates a
    feed, and it deliberately does nothing while a job is young — the whole point is
    to give the better model its chance first.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    narrator = _narrator_mod()
    now = datetime.now(_tz())
    stamp = now.isoformat(timespec="seconds")
    cutoff_s = COACH_SONNET_WAIT_HOURS * 3600

    handled, skipped = [], []
    for job in store.list_jobs():
        age = store._age_seconds(job.get("created_at"), stamp)
        if age is None or age < cutoff_s:
            skipped.append(job.get("id"))
            continue
        if not _gemini_available():
            return jsonify({"error": "GEMINI_API_KEY not configured"}), 503
        try:
            answer = narrator.call_gemini(job["prompt"],
                                          require_key=job.get("require_key", "cards"),
                                          temperature=0.35)
        except narrator.GeminiQuotaError as exc:
            app.logger.warning("sweep hit quota on %s: %s", job.get("id"), exc)
            return jsonify({"status": "quota", "handled": handled}), 503
        except Exception:
            app.logger.exception("sweep generation failed for %s", job.get("id"))
            continue
        if job.get("chat"):
            _apply_chat(job, answer, now, source="gemini")
        elif job.get("report"):
            _apply_report(job, answer, now, source="gemini")
        else:
            _apply_generation(job, answer, now, source="gemini")
        store.finish_generation_job(job["id"])
        handled.append(job["id"])

    return jsonify({"status": "swept", "fell_back_to_gemini": handled,
                    "still_waiting_for_sonnet": skipped}), 200


def _coach_weekly_facts(profile: Dict[str, Any]) -> Dict[str, Any]:
    """The Sunday frame: the week in foods, plus the continuity thread against the
    finding the last review picked (a coach that remembers what it asked for is a
    relationship; one that starts fresh every week is a report)."""
    store = _coach("coach_store")
    state = store.read_state()
    shown = state.get("shown") or {}
    previous = sorted(shown.items(), key=lambda kv: str(kv[1].get("date") or ""),
                      reverse=True)[:1]
    prior = None
    if previous:
        key, record = previous[0]
        current = next((f for f in profile.get("findings", []) if f["id"] == key),
                       None)
        prior = {
            "finding": key,
            "severity_then": record.get("severity"),
            "severity_now": current["severity"] if current else 0.0,
            "still_present": bool(current),
        }
    return {"prior_focus": prior,
            "groups": profile.get("groups"),
            "variety": profile.get("variety")}


# -- chat ----------------------------------------------------------------------
#
# Chat is QUEUED WORK, not a request that waits for a model — the same shape as the
# feed and the reports, and for a reason the old synchronous version demonstrated in
# production: a slow model call inside the request meant the app's 60 s timeout fired
# while the server was still working, the client retried (every POST was retried, on
# the assumption they were all idempotent), and each retry ran the whole thing again.
# One question became three questions and three different answers, all saved.
#
# So: the question is recorded and parked, Sonnet answers it whenever the Mac is
# awake and inside its allowance, and the app picks the answer up next time it looks.
# Nothing is lost if the user closes the app, and the reply is worth the wait rather
# than whatever could be produced inside an HTTP timeout.
#
# Sonnet at MEDIUM effort: a chat turn is a couple of sentences grounded in facts that
# are already in the prompt — it needs the good voice, not the deep reasoning a weekly
# review gets, and medium keeps the answer minutes away rather than tens of minutes.
COACH_CHAT_MODEL = os.environ.get("COACH_CHAT_MODEL", "claude-sonnet-5")
COACH_CHAT_EFFORT = os.environ.get("COACH_CHAT_EFFORT", "medium")
COACH_CHAT_TIMEOUT_S = int(os.environ.get("COACH_CHAT_TIMEOUT_S", "300"))


def _thread(thread_id: str) -> Dict[str, Any]:
    store = _coach("coach_store")
    return store.read_json(store.thread_path(thread_id),
                           default={"id": thread_id, "turns": []})


def _pending_chat_turn_ids(thread_id: str) -> List[str]:
    """The client turn ids this thread has questions queued for.

    Read off the job queue rather than stored on the thread, so it can never go stale:
    a job that is finished, swept or abandoned simply stops appearing here.
    """
    store = _coach("coach_store")
    out: List[str] = []
    for job in store.list_jobs():
        chat = job.get("chat")
        if isinstance(chat, dict) and chat.get("thread_id") == thread_id \
                and not job.get("done_at"):
            out.append(str(chat.get("turn_id") or ""))
    return [t for t in out if t]


def _thread_out(thread_id: str) -> Dict[str, Any]:
    """A thread plus whether it is still waiting on an answer, so the app can show a
    question as in-flight instead of as unanswered."""
    thread = dict(_thread(thread_id))
    pending = _pending_chat_turn_ids(thread_id)
    thread["pending"] = bool(pending)
    thread["pending_turn_ids"] = pending
    return thread


@app.get("/coach/thread/<thread_id>")
def coach_thread(thread_id: str):
    """One conversation, oldest turn first, with its pending state."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    if not store.is_safe_id(thread_id):
        return jsonify({"error": "bad thread id"}), 400
    return jsonify(_thread_out(thread_id)), 200


@app.post("/coach/chat")
def coach_chat():
    """Ask the coach something about a card. Returns as soon as the question is
    recorded — the answer arrives later, from Sonnet.

    IDEMPOTENT on `client_turn_id`. That is what actually makes the duplicate bug
    impossible rather than merely unlikely: the app sends one id per message it
    composes, so a retry — a lost response, a flaky connection, a double tap, a
    background relaunch — lands on the same id and is recognised as the same
    question. Without it, moving the model out of the request path would still leave
    a retried POST queueing two jobs.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    store = _coach("coach_store")
    memory_mod = _coach("coach_memory")
    narrator = _narrator_mod()

    body = request.get_json(silent=True) or {}
    message = " ".join(str(body.get("message") or "").split())[:1000]
    thread_id = str(body.get("thread_id") or "").strip()
    card_id = str(body.get("card_id") or "").strip()
    turn_id = str(body.get("client_turn_id") or "").strip()[:64]
    # The request is validated before anything else, so a malformed call reads as a
    # 400 rather than as a backend failure.
    if not message:
        return jsonify({"error": "message is required"}), 400
    if not store.is_safe_id(thread_id):
        return jsonify({"error": "bad thread id"}), 400
    if not turn_id:
        # An older build that doesn't send one still works; it just doesn't get the
        # duplicate protection, so it is given an id here rather than refused.
        turn_id = uuid.uuid4().hex
    elif not store.is_safe_id(turn_id):
        return jsonify({"error": "bad client_turn_id"}), 400

    now = datetime.now(_tz())
    stamp = now.isoformat(timespec="seconds")
    thread = _thread(thread_id)

    # -- the two idempotency checks, cheapest first ----------------------------
    already_asked = any(str(t.get("turn_id") or "") == turn_id
                        for t in (thread.get("turns") or [])
                        if isinstance(t, dict))
    if already_asked:
        app.logger.info("chat turn %s already recorded — not queueing again", turn_id)
        return jsonify({"status": "already-asked", **_thread_out(thread_id)}), 200
    if turn_id in _pending_chat_turn_ids(thread_id):
        app.logger.info("chat turn %s already queued", turn_id)
        return jsonify({"status": "already-queued", **_thread_out(thread_id)}), 202

    card = next((c for c in _read_feed_cards(now) if c.get("id") == card_id), None)

    try:
        window_meals, today_rows = _coach_window_meals(now)
        consumed = _todays_consumed(today_rows)
        targets, _basis = _resolved_targets_and_basis()
        taxonomy = _display_taxonomy()
        profile = _coach_profile(now, window_meals)
        memory = store.read_json(store.MEMORY, default=memory_mod.empty())
        context = {
            "card": {"title": card.get("title"), "body": card.get("body"),
                     "kind": card.get("kind"), "evidence": card.get("evidence"),
                     "swap": card.get("swap")} if card else None,
            "today": _coach_today(now, today_rows, consumed, targets,
                                  taxonomy=taxonomy),
            "nutrients_supporting": _coach_nutrients(consumed, targets),
            "food_patterns": {
                "groups": {k: {"label": v.get("label"),
                               "servings_per_week": v.get("servings_per_week"),
                               "reference_min": v.get("week_min"),
                               "reference_max": v.get("week_max"),
                               "days_since_last": v.get("days_since_last")}
                           for k, v in (profile.get("groups") or {}).items()},
                "variety": profile.get("variety"),
                "findings": [{"fact": f["headline"]}
                             for f in profile.get("findings", [])[:5]],
            },
            # pt-PT, like every other prompt payload — the chat prompt is Portuguese
            # and quotes these names straight back to the user.
            "foods_the_user_eats": [
                {"food": f.get("pt") or f["food"], "times": f["times"],
                 "typical_portion_g": f["median_portion_g"]}
                for f in profile.get("foods", [])[:25]],
            "memory": memory_mod.for_prompt(memory),
        }
        prompt = narrator.build_chat_prompt(context, thread.get("turns") or [],
                                            message)
    except Exception as exc:
        app.logger.exception("coach chat preparation failed")
        return jsonify({"status": "error", "error": str(exc)}), 500

    # The question goes into the transcript NOW, before the job exists. If queueing
    # fails the user still sees what they asked and can ask again; if the order were
    # reversed, a job could answer a question the thread had no record of.
    def add_question(current: Any) -> Dict[str, Any]:
        data = current if isinstance(current, dict) else {"id": thread_id,
                                                          "turns": []}
        turns = [t for t in (data.get("turns") or []) if isinstance(t, dict)]
        if not any(str(t.get("turn_id") or "") == turn_id for t in turns):
            turns.append({"role": "user", "text": message, "at": stamp,
                          "turn_id": turn_id})
        data.update({"id": thread_id, "card_id": card_id or data.get("card_id"),
                     "title": (card or {}).get("title") or data.get("title"),
                     "updated_at": stamp, "turns": turns[-60:]})
        return data
    store.update_json(store.thread_path(thread_id), add_question,
                      default={"id": thread_id, "turns": []})

    job = {
        "id": f"{now.strftime('%Y%m%dT%H%M%S')}-chat-{turn_id[:8]}",
        "slot": "chat",
        "reason": "chat turn",
        "created_at": stamp,
        "claimed_at": None,
        "claimed_by": None,
        "attempts": 0,
        "prompt": prompt,
        "require_key": "reply",
        "model": COACH_CHAT_MODEL,
        "effort": COACH_CHAT_EFFORT,
        # Without this the worker infers "any job naming a model is a report" and
        # gives a two-sentence answer the 15-minute report budget.
        "timeout_s": COACH_CHAT_TIMEOUT_S,
        "chat": {"thread_id": thread_id, "card_id": card_id, "turn_id": turn_id,
                 "message": message,
                 "card_title": str((card or {}).get("title") or "")},
    }
    store.write_json(store.job_path(job["id"]), job)
    app.logger.info("chat job %s queued (thread=%s, %d chars)", job["id"],
                    thread_id, len(prompt))

    return jsonify({"status": "queued", "job": job["id"],
                    "waiting_for": "sonnet", **_thread_out(thread_id)}), 202


def _apply_chat(job: Dict[str, Any], answer: Dict[str, Any], now: datetime, *,
                source: str) -> Dict[str, Any]:
    """Fold one answered chat turn back into its thread.

    Everything the old synchronous endpoint did after the model returned — append,
    archive, harvest memory — lives here now, so Sonnet and the Gemini fallback land
    in exactly the same place.
    """
    store = _coach("coach_store")
    memory_mod = _coach("coach_memory")
    chat = job.get("chat") or {}
    thread_id = str(chat.get("thread_id") or "")
    turn_id = str(chat.get("turn_id") or "")
    reply = str(answer.get("reply") or "").strip()
    if not thread_id or not reply:
        app.logger.warning("chat job %s produced no usable reply", job.get("id"))
        return {"chat": 0, "source": source}

    stamp = now.isoformat(timespec="seconds")

    def add_answer(current: Any) -> Dict[str, Any]:
        data = current if isinstance(current, dict) else {"id": thread_id,
                                                          "turns": []}
        turns = [t for t in (data.get("turns") or []) if isinstance(t, dict)]
        # Answering the same turn twice is possible in one narrow case — the sweeper
        # falling back to Gemini for a job the worker was already mid-way through —
        # so the reply is keyed to its question the same way the question is.
        if any(t.get("role") == "coach" and str(t.get("reply_to") or "") == turn_id
               for t in turns):
            return data
        turns.append({"role": "coach", "text": reply, "at": stamp,
                      "reply_to": turn_id, "source": source})
        data.update({"id": thread_id, "updated_at": stamp, "turns": turns[-60:]})
        return data
    store.update_json(store.thread_path(thread_id), add_answer,
                      default={"id": thread_id, "turns": []})

    try:
        _coach("coach_archive").record_chat(
            thread_id, day=now.date().isoformat(), at=stamp[11:16],
            question=str(chat.get("message") or ""), answer=reply,
            card_title=str(chat.get("card_title") or ""))
    except Exception:
        app.logger.exception("archiving chat failed (non-fatal)")

    learned = 0
    candidates = answer.get("memory_candidates") or []
    if candidates:
        def remember(current: Any) -> Dict[str, Any]:
            merged, added = memory_mod.merge(
                current if isinstance(current, dict) else memory_mod.empty(),
                candidates, today=now.date().isoformat(), source="chat")
            merged["_added"] = added
            return merged
        stored = store.update_json(store.MEMORY, remember,
                                   default=memory_mod.empty())
        learned = int((stored or {}).pop("_added", 0) or 0)
        if learned:
            app.logger.info("coach memory: %d new fact(s) from chat", learned)

    app.logger.info("chat job %s answered via %s", job.get("id"), source)
    return {"chat": 1, "source": source, "memory_learned": learned}



# -- memory --------------------------------------------------------------------

@app.get("/coach/memory")
def coach_memory_read():
    """What the coach remembers, newest-weightiest first."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    memory_mod = _coach("coach_memory")
    memory = store.read_json(store.MEMORY, default=memory_mod.empty())
    return jsonify(memory), 200


@app.post("/coach/memory")
def coach_memory_add():
    """Tell the coach something about yourself directly. Pinned, because it was said
    on purpose rather than inferred from a conversation."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    memory_mod = _coach("coach_memory")
    body = request.get_json(silent=True) or {}
    fact = " ".join(str(body.get("fact") or "").split())[:160]
    if not fact:
        return jsonify({"error": "fact is required"}), 400
    kind = str(body.get("type") or "preference")
    today_iso = datetime.now(_tz()).date().isoformat()
    memory = store.update_json(
        store.MEMORY,
        lambda current: memory_mod.add_manual(current, kind=kind, fact=fact,
                                              today=today_iso),
        default=memory_mod.empty())
    return jsonify(memory), 200


@app.delete("/coach/memory/<fact_id>")
def coach_memory_delete(fact_id: str):
    """Forget one fact. The coach being wrong about you is recoverable; the coach
    being permanently wrong about you is not."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    store = _coach("coach_store")
    memory_mod = _coach("coach_memory")
    if not store.is_safe_id(fact_id):
        return jsonify({"error": "bad id"}), 400
    memory = store.update_json(
        store.MEMORY, lambda current: memory_mod.remove(current, fact_id),
        default=memory_mod.empty())
    return jsonify(memory), 200


@app.get("/coach/patterns")
def coach_patterns():
    """The deterministic food-level analysis, unnarrated — the debug view for
    eyeballing what the coach is reasoning over before any model call is spent (the
    same role `/insights/diagnose` plays for the nutrient side)."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    now = datetime.now(_tz())
    window_meals, _today_rows = _coach_window_meals(now)
    return jsonify(_coach_profile(now, window_meals)), 200


# =============================================================================
# Reports: the consolidated tier.
#
# The daily feed answers "what now". These answer "what has been happening", which is
# a different job needing a different model and a different amount of thinking. Each
# level reads only the level below — weekly reads the week, monthly reads four or five
# weeklies, yearly reads twelve monthlies — so writing any of them costs about the
# same no matter how many years of history exist. That hierarchy is what makes keeping
# everything affordable rather than merely possible.
# =============================================================================

# Reports go to the strongest model at a slower, more thorough setting. This is the
# one place in the coach where the reasoning is genuinely hard: correlating what was
# advised against what was eaten, over weeks, and saying something true about the
# direction of travel.
# The ALIAS, not a pinned version. `claude-opus-5` is not resolvable by the
# subscription CLI on this machine ("it may not exist or you may not have access"),
# while `opus` resolves to whatever the best available Opus is — currently
# claude-opus-4-8. Pinning a name the account cannot reach fails the job hard; the
# alias follows the plan.
COACH_REPORT_MODEL = os.environ.get("COACH_REPORT_MODEL", "opus")
COACH_REPORT_EFFORT = os.environ.get("COACH_REPORT_EFFORT", "medium")


@app.post("/coach/report")
def coach_report():
    """Prepare a weekly, monthly or yearly review as a job for the strong model.

    `{"period": "weekly|monthly|yearly"}`, optionally `{"ref": "YYYY-MM-DD"}` to
    re-run a past period. Like `/coach/generate`, this calls no model: it gathers the
    facts, builds the prompt and parks the job.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    reports_mod = _coach("coach_reports")
    store = _coach("coach_store")

    body = request.get_json(silent=True) or {}
    period = str(body.get("period") or "weekly")
    if period not in reports_mod.PERIODS:
        return jsonify({"error": f"unknown period {period!r}",
                        "known": list(reports_mod.PERIODS)}), 400
    now = datetime.now(_tz())
    try:
        ref = (datetime.fromisoformat(str(body["ref"])).date()
               if body.get("ref") else now.date())
    except (TypeError, ValueError):
        return jsonify({"error": "ref must be YYYY-MM-DD"}), 400

    try:
        job = _build_report_job(now, period=period, ref=ref)
    except Exception as exc:
        app.logger.exception("report job preparation failed")
        return jsonify({"status": "error", "error": str(exc)}), 500
    if job is None:
        return jsonify({"status": "empty", "period": period,
                        "reason": "nothing logged in that period"}), 200

    store.write_json(store.job_path(job["id"]), job)
    app.logger.info("coach report job %s queued (%d chars)", job["id"],
                    len(job["prompt"]))
    return jsonify({"status": "queued", "job": job["id"], "period": period,
                    "covering": job["report"], "waiting_for": "opus"}), 202


def _build_report_job(now: datetime, *, period: str, ref) -> Optional[Dict[str, Any]]:
    """Gather a period's facts and build its prompt.

    A weekly reads the raw week — every meal, every card, every conversation, every
    event. A monthly or yearly reads only the reports beneath it, which is what keeps
    the prompt a page rather than a phone book.
    """
    archive = _coach("coach_archive")
    reports_mod = _coach("coach_reports")
    memory_mod = _coach("coach_memory")
    store = _coach("coach_store")
    patterns = _coach("food_patterns")

    start, end, key = reports_mod.period_bounds(period, ref)
    memory = store.read_json(store.MEMORY, default=memory_mod.empty())
    memory_facts = memory_mod.for_prompt(memory, limit=20)

    if period == "weekly":
        all_rows = _all_meal_rows()
        rows = _window_meals(all_rows, start, end)
        taxonomy = store.read_json(store.TAXONOMY, default=None)
        meals = patterns.read_meals(rows, taxonomy)
        if not meals:
            return None
        profile = patterns.build_food_profile(
            rows, taxonomy=taxonomy,
            window_days=7, ref_day=end)
        previous = archive.recent_reports("weekly", before=key, limit=1)
        caps = _capabilities()
        # The links need their full window to have any statistical standing — seven
        # days would clear no hypothesis's minimum n — so the engine reads its own
        # window and the report shows the week's rows alongside what it found.
        metric_window = _coach_window_days(now)
        week_days = [d for d in metric_window
                     if start <= str(d.get("date", "")) <= end]
        facts = reports_mod.weekly_facts(
            start=start, end=end, key=key, meals=meals, profile=profile,
            archive_entries=archive.read_range(start, end),
            previous=previous[0] if previous else None,
            memory_facts=memory_facts,
            metric_findings=_coach_metric_findings(
                metric_window, caps, _meals_by_waking_day(rows)),
            days=week_days, capabilities=caps.to_api())
    else:
        child = "weekly" if period == "monthly" else "monthly"
        children = [r for r in archive.recent_reports(child, before="9999", limit=14)
                    if start <= str(r.get("covering", {}).get("from") or
                                    r.get("key") or "") <= end]
        if not children:
            return None
        window_meals, _today = _coach_window_meals(now)
        facts = reports_mod.rollup_facts(
            period=period, start=start, end=end, key=key,
            children=sorted(children, key=lambda r: str(r.get("key"))),
            profile=_coach_profile(now, window_meals), memory_facts=memory_facts)

    return {
        "id": f"{now.strftime('%Y%m%dT%H%M%S')}-{period}-{key}",
        "slot": "report",
        "reason": f"{period} report",
        "created_at": now.isoformat(timespec="seconds"),
        "claimed_at": None,
        "claimed_by": None,
        "attempts": 0,
        "prompt": reports_mod.build_prompt(facts),
        "require_key": "headline",
        # Routed to the strong model, and the worker reads these two fields to know
        # which one to run.
        "model": COACH_REPORT_MODEL,
        "effort": COACH_REPORT_EFFORT,
        "report": {"period": period, "key": key, "from": start, "to": end},
    }


def _apply_report(job: Dict[str, Any], answer: Dict[str, Any], now: datetime, *,
                  source: str) -> Dict[str, Any]:
    """Store a finished report and put its headline on the feed."""
    archive = _coach("coach_archive")
    reports_mod = _coach("coach_reports")
    feed = _coach("coach_feed")
    store = _coach("coach_store")

    meta = job.get("report") or {}
    period, key = meta.get("period", "weekly"), meta.get("key", "")
    report = reports_mod.assemble_report(
        answer, period=period, key=key, start=meta.get("from", ""),
        end=meta.get("to", ""), now=now, source=source)
    if not report.get("headline") and not report.get("summary"):
        return {"report": None}

    archive.save_report(period, key, report)
    archive.record_report(period, key, day=now.date().isoformat(),
                          headline=report.get("headline", ""),
                          topics=[period])

    fields = reports_mod.as_card_fields(report)
    card = feed._card(kind=fields["kind"], slot="weekly",
                      date=now.date().isoformat(), now=now,
                      title=fields["title"], body=fields["body"],
                      topic=f"{period}:{key}", chips=fields["chips"],
                      evidence={"period": period, "key": key,
                                "covering": meta.get("from"),
                                "focus": report.get("focus")})
    card["source"] = source
    stamp = now.isoformat(timespec="seconds")

    def merge_into(current: Any) -> Dict[str, Any]:
        existing = (current.get("cards") or []) if isinstance(current, dict) else []
        return {"generated_at": stamp, "slot": "report", "source": source,
                "cards": feed.merge_cards(existing, [card], now=now)}
    store.update_json(store.FEED, merge_into, default={"cards": []})
    archive.record_cards([card], now=now)

    app.logger.info("coach %s report %s written by %s", period, key, source)
    return {"report": key, "period": period, "source": source}


@app.get("/coach/reports")
def coach_reports_list():
    """Past reports, newest first — what the app's history screen reads.
    `?period=weekly|monthly|yearly`, `?key=` for one in full."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    archive = _coach("coach_archive")
    period = str(request.args.get("period") or "weekly")
    if period not in ("weekly", "monthly", "yearly"):
        return jsonify({"error": "unknown period"}), 400
    key = str(request.args.get("key") or "").strip()
    if key:
        store = _coach("coach_store")
        if not store.is_safe_id(key):
            return jsonify({"error": "bad key"}), 400
        report = archive.load_report(period, key)
        return (jsonify(report), 200) if report else (
            jsonify({"error": "no such report"}), 404)
    return jsonify({"period": period,
                    "reports": archive.recent_reports(period, before="9999",
                                                      limit=24)}), 200


@app.get("/coach/history")
def coach_history():
    """Everything the coach has said and noticed, newest first.

    `?from=&to=` (default: the last 30 days), `?kinds=card,chat,event,report`. This is
    the app's history screen and the honest answer to "is it really keeping all of
    this".
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    archive = _coach("coach_archive")
    now = datetime.now(_tz())
    end = str(request.args.get("to") or now.date().isoformat())
    start = str(request.args.get("from")
                or (now.date() - timedelta(days=30)).isoformat())
    for value in (start, end):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return jsonify({"error": "from/to must be YYYY-MM-DD"}), 400
    kinds = tuple(k.strip() for k in str(request.args.get("kinds") or "").split(",")
                  if k.strip())
    entries = archive.read_range(start, end, kinds=kinds)
    entries.reverse()
    return jsonify({"from": start, "to": end, "count": len(entries),
                    "entries": entries[:400]}), 200


@app.get("/coach/archive-stats")
def coach_archive_stats():
    """What the archive holds, by month and kind — the debug view for the memory."""
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(_coach("coach_archive").stats()), 200
