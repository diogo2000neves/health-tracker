#!/usr/bin/env python3
"""THE single place that decides which model answers a given prompt.

Both `backend/ingest/` and `automation/coach/worker.py` call `call_json()` below
and neither knows, or needs to know, which CLI actually ran. Routing is by the
caller's own `source` string — the one every call already passes and every line of
`logs/calls.jsonl` already records — so the task taxonomy and the routing table are
the same vocabulary rather than two that can drift.

## Three layers, and none of them asks the user anything mid-flow

The requirement that shaped this: *"sometimes I send a photo and my Claude window
is already spent, or I want to save it because I'll need it later today."* Those are
two different problems and they get two different mechanisms.

1. **Per-task defaults (`ROUTES`).** Transcription — reading numbers off a scale
   or a gym screenshot — belongs on Gemini: the answer is *on the screen* and a
   plausibility band verifies it downstream, so a stronger model buys nothing but
   latency. Judgement — estimating a meal from a photo, writing the coach's prose
   — stays on Claude. A photo cannot be classified before it is routed, so
   `main._classify_images` makes one cheap `fast` call that returns only `kind`
   and routes on that; anything short of a confident screenshot verdict stays
   `deep`.
2. **Automatic failover (`TIERS`).** Each tier is an ORDERED list of providers, not
   one model. A spent usage window, a timeout or an unparseable answer moves to the
   next provider by itself. This is the "I already burned my Claude" case, and it
   needs no decision from anyone.
3. **A deliberate mode (`resolve_mode`).** `economy` forces every tier onto Gemini
   for as long as it is set; `quality` pins the first provider and refuses to
   silently downgrade. Read from the sheet's `config` tab (see
   `ingest/main.py:_llm_mode`) so it is one cell on a phone, not a deploy.

Precedence, in the order a call resolves:

    mode == economy   -> the tier's Gemini providers only
    mode == quality   -> the tier's first provider only, no failover
    claude in cooldown-> skip it, start at the next provider
    otherwise         -> the tier's full chain, in order

## The cooldown

When Claude reports a spent 5-hour window, every following call in that window
costs ~30 s of subprocess before failing the same way. `note_failure()` arms a
short cooldown so the chain starts at Gemini instead.

It is armed by ANY Claude failure, not only a usage-limit one, and that is
deliberate. `claude_cli` collapses every failure into one `ClaudeError`, so telling
"window spent" from "transport blip" means sniffing the error string — and this repo
already learned that lesson expensively on the Gemini side, where a quota error's
details JSON contains the number 400 and a substring sniff read it as a permanent
bad request (see `main._retry_same_model`, which classifies on `APIError.code` for
exactly this reason). Since the fallback is a strong model on a subscription that is
already paid for, guessing wrong costs one session of slightly different prose. Not
worth a fragile classifier.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import agy_cli
import claude_cli

# -- providers -----------------------------------------------------------------
CLAUDE, AGY = "claude", "agy"


@dataclass(frozen=True)
class Provider:
    """One way to get an answer. `effort` is only consulted for the claude path —
    agy bakes the effort into the model id (…-high/-medium/-low)."""
    kind: str
    model: str
    effort: str = ""

    @property
    def is_gemini(self) -> bool:
        return self.kind == AGY


# Claude first for judgement, agy second. Both run on subscriptions that are
# already paid, so this ordering is about accuracy and about protecting the 5-hour
# window — never about money. The Gemini *API* (main.DEFAULT_MODELS) sits behind
# both as the last resort, reached only when this whole chain has failed; it is a
# net, not a choice.
DEEP: Tuple[Provider, ...] = (
    Provider(CLAUDE, "claude-sonnet-5", "high"),
    Provider(AGY, "gemini-3.7-flash", "high"),
)
# Transcription. agy leads: the answer is on the screen, and the seconds saved are
# 5-hour-window slots kept for the work that actually needs judgement.
#
# MEDIUM, not low, and the distinction is not fussiness: a gym screen is a dense
# grid of near-identical rows, and a misread rep count (8 vs 3) lands INSIDE
# `workouts.SET_RANGES` — so unlike a dropped decimal on the scale, the
# plausibility band cannot catch it. Effort is the only guard there is.
FAST: Tuple[Provider, ...] = (
    Provider(AGY, "gemini-3.7-flash", "medium"),
    Provider(CLAUDE, "claude-sonnet-5", "low"),
)
# One question, one word back: "is this a plate, a scale, or a gym screen?".
# Nothing downstream trusts it beyond picking a tier, and a wrong answer costs a
# tier rather than a row (see main._classify_images), so low is right.
CLASSIFY: Tuple[Provider, ...] = (
    Provider(AGY, "gemini-3.7-flash", "low"),
    Provider(CLAUDE, "claude-sonnet-5", "low"),
)
# Text-only notes from the "send a note" shortcut. Still judgement — the model
# estimates a meal from words alone, same task as DEEP — so this keeps high effort
# and a Claude fallback; the only change from DEEP is which provider goes first.
# Diogo's notes are short and specific ("100g de arroz e um ovo cozido"), not a
# photo needing Claude's edge at reading a plate, so Gemini answers them just as
# well for a fraction of the 5-hour window.
NOTE: Tuple[Provider, ...] = (
    Provider(AGY, "gemini-3.7-flash", "high"),
    Provider(CLAUDE, "claude-sonnet-5", "high"),
)
# The daily/weekly coach card only — chat and report keep pinning their own model
# (see worker.answer) and never reach this tier. worker.py's own docstring calls
# the coaching voice "the product" and the card is not exempt from that; this is a
# deliberate trade Diogo chose anyway after seeing Gemini hold up on NOTE, so it
# stays high effort and still falls back to Claude rather than silently degrading.
CARD: Tuple[Provider, ...] = (
    Provider(AGY, "gemini-3.7-flash", "high"),
    Provider(CLAUDE, "claude-sonnet-5", "high"),
)

TIERS: Dict[str, Tuple[Provider, ...]] = {
    "deep": DEEP, "fast": FAST, "classify": CLASSIFY, "note": NOTE, "card": CARD,
}
DEFAULT_TIER = "deep"

# Model ids as `agy models` lists them already carry the effort
# (`gemini-3.7-flash-high`); a BARE id needs `--effort` and errors without it.
# `agy_cli._effort_args` handles both, so the bare id + explicit effort above is
# deliberate: it keeps `effort` a first-class field for every provider rather than
# a suffix hidden inside a string, and it survives Google renaming the suffixes
# again (which is how this broke on the first live call, 2026-08-21).

# Which tier each caller gets. The key is the `source` string the caller already
# passes for the call log. An unknown source falls to DEFAULT_TIER: a new call site
# that nobody has classified yet should get the good model and show up as a cost,
# not silently get the cheap one and show up as a quality regression.
ROUTES: Dict[str, str] = {
    # -- judgement: the answer is not in the image, so a stronger model earns its
    #    latency. The meal estimate is the load-bearing one — CONTEXT.md §2e has
    #    MEASURED a ~250 kcal/day systematic bias already, and this is the number
    #    the whole project exists to find.
    "ingest.claude_estimator": "deep",
    "ingest.meal_estimate": "deep",
    # Text-only note ("send a note" shortcut, no photo attached) — see NOTE above
    # for why this is the one judgement task that leads with Gemini instead.
    "ingest.text_note": "note",
    "coach": "deep",
    # The daily/weekly card — see CARD above for why this one leads with Gemini
    # while chat and report (pinned models, bypass routing) do not.
    "coach.card": "card",
    "coach.report": "deep",
    "audit.estimate": "deep",
    "audit.adjudicate": "deep",
    "audit.ground": "deep",

    # -- transcription: the answer is literally on the screen, and a plausibility
    #    band downstream (`_normalize_body`, `workouts.normalize_sets`) verifies it
    #    either way, so a stronger model buys nothing but latency and a 5-hour
    #    window slot.
    #
    #    Reached through the classify-then-route split: `main._classify_images`
    #    makes one cheap `fast` call that returns only `kind`, and the real call
    #    then runs at the tier that verdict implies. The asymmetry there is the
    #    safety property — only a confident "body"/"workout" may downgrade, and
    #    anything else (including a failed classify) stays `deep`, because a
    #    screenshot on the deep tier costs a little window while a MEAL on the fast
    #    tier costs accuracy on the number the system exists to measure.
    "ingest.image_router": "classify",
    "ingest.body_ocr": "fast",
    "ingest.workout_ocr": "fast",
}

MODES = ("auto", "economy", "quality")
DEFAULT_MODE = "auto"

# How long a Claude failure keeps the chain starting at agy. Sized against the
# subscription's 5-hour window: long enough that a spent window doesn't cost a
# failed subprocess per meal, short enough that a transport blip doesn't hand the
# rest of the afternoon to the fallback.
COOLDOWN_S = int(os.environ.get("LLM_CLAUDE_COOLDOWN_S", "1800"))

# Back-compat with the single-switch era. Setting PRIMARY_MODEL still overrides
# everything above — it is the instant rollback, and callers that pass an explicit
# `model=` (the audit pins its own) keep bypassing routing entirely.
PRIMARY_MODEL = os.environ.get("PRIMARY_MODEL", "")
PRIMARY_EFFORT = os.environ.get("PRIMARY_EFFORT", "high")

_cooldown_until: float = 0.0


def resolve_mode(mode: Optional[str] = None) -> str:
    """The active mode: the caller's, else `LLM_MODE`, else auto. A typo reads as
    auto rather than raising — this value comes from a hand-edited spreadsheet
    cell, and the worst a typo may do is leave routing at its default."""
    raw = (mode or os.environ.get("LLM_MODE") or DEFAULT_MODE).strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def tier_for(source: str) -> str:
    return ROUTES.get((source or "").strip(), DEFAULT_TIER)


def note_failure(provider: Provider) -> None:
    """Arm the cooldown after a Claude failure. See the module docstring on why any
    failure counts, not just a usage-limit one."""
    global _cooldown_until
    if provider.kind == CLAUDE:
        _cooldown_until = time.monotonic() + COOLDOWN_S


def claude_in_cooldown() -> bool:
    return time.monotonic() < _cooldown_until


def reset_cooldown() -> None:
    """Test hook, and the thing to call after a successful Claude answer."""
    global _cooldown_until
    _cooldown_until = 0.0


def chain_for(source: str, mode: Optional[str] = None) -> List[Provider]:
    """The providers this call may use, in order.

    Never returns empty: a mode that filters everything out falls back to the
    tier's own order, because answering with the "wrong" provider beats not
    answering at all — the caller's alternative is a failed meal, not a better one.
    """
    providers = list(TIERS.get(tier_for(source), TIERS[DEFAULT_TIER]))
    resolved = resolve_mode(mode)

    if resolved == "economy":
        return [p for p in providers if p.is_gemini] or providers
    if resolved == "quality":
        return providers[:1]

    if claude_in_cooldown() and any(p.kind != CLAUDE for p in providers):
        return [p for p in providers if p.kind != CLAUDE]
    return providers


def _call_one(provider: Provider, prompt: str, *, timeout_s: int, source: str,
              require_key: str, tools: str, cwd: Optional[str]) -> Dict[str, Any]:
    if provider.is_gemini:
        return agy_cli.call_agy_json(prompt, model=provider.model,
                                     timeout_s=timeout_s, source=source,
                                     require_key=require_key,
                                     effort=provider.effort, cwd=cwd)
    return claude_cli.call_claude_json(prompt, model=provider.model,
                                       effort=provider.effort,
                                       timeout_s=timeout_s, source=source,
                                       require_key=require_key, tools=tools)


def call_json(prompt: str, *, model: Optional[str] = None,
              effort: Optional[str] = None, timeout_s: int, source: str,
              require_key: str = "items", tools: str = "Read",
              cwd: Optional[str] = None,
              mode: Optional[str] = None) -> Dict[str, Any]:
    """Answer one prompt, walking this source's provider chain until one succeeds.

    Raises `claude_cli.ClaudeError` when every provider in the chain has failed —
    the same exception the single-provider version raised, so every existing caller
    (ingest's fall-through-on-any-exception, worker.py's release-the-job) keeps
    working unchanged and simply falls to the Gemini API behind us.

    An explicit `model=` bypasses routing entirely, which is what the audit needs:
    its ensemble is only meaningful if it can pin each estimate to a named model.
    """
    explicit = model or PRIMARY_MODEL
    if explicit:
        # The effort matters for BOTH providers here. It used to be dropped on the
        # agy branch, which made a pinned bare id (`PRIMARY_MODEL=gemini-3.7-flash`)
        # fail at the CLI with "requires --effort" — the rollback switch breaking
        # exactly when it is reached for.
        provider = Provider(AGY if explicit.startswith("gemini") else CLAUDE,
                            explicit, effort or PRIMARY_EFFORT)
        return _call_one(provider, prompt, timeout_s=timeout_s, source=source,
                         require_key=require_key, tools=tools, cwd=cwd)

    last: Optional[Exception] = None
    for provider in chain_for(source, mode):
        try:
            answer = _call_one(provider, prompt, timeout_s=timeout_s,
                               source=source, require_key=require_key,
                               tools=tools, cwd=cwd)
        except claude_cli.ClaudeError as err:
            last = err
            note_failure(provider)
            continue
        if provider.kind == CLAUDE:
            reset_cooldown()
        return answer
    raise claude_cli.ClaudeError(
        f"every provider failed for {source!r}; last error: {last}")
