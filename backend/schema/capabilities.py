"""What this user actually measures — the one switch the whole app reads.

The app started as one person's dashboard with three sensors behind it. It is meant
to also work for someone who owns nothing but a phone camera, without a second
codebase and without an `if friend:` in every function. This module is how.

## Why blocks, and not a level

The obvious shape is a ladder — level 1 nutrition, level 2 add sleep, level 3
everything. It breaks on the first real person: a friend with a watch but no scale
is not "level 2.5". So the unit is the **block**, the vocabulary `registry.BLOCKS`
already defines, and a capability is simply *the set of blocks this user has data
for*. Every combination is expressible and they all cost the same.

`PRESETS` then gives back the "one simple switch" feel — `nutrition`, `+sleep`,
`full` — as names for common block sets, never as a rank.

## What follows from it, for free

Because every column in the registry declares its `source` and its `block`,
everything downstream is derived rather than configured:

* which sheet columns can ever be filled  -> `columns()`
* which integrations are worth running    -> `sources()` (no health OAuth for a
  nutrition-only user, so the daily job simply has nothing to fetch)
* which API blocks may be served          -> `visible_blocks()`
* which coach domains may speak           -> `domains()`
* which cross-domain links are evaluable  -> a link needs BOTH its blocks enabled,
  so a nutrition-only user gets none, silently and correctly.

## The declared profile, and why it exists

The owner's calorie target is derived from a *measured* TDEE and a *measured*
weight. Someone with no watch and no scale has neither, and the old fallbacks
(`DEFAULT_TDEE`, `DEFAULT_WEIGHT_KG`) are placeholders, not a person. So a
capability also carries what the user can simply *tell* us — sex, age, height,
weight, activity level — used strictly as the lowest layer:

    manual override  >  measured  >  declared  >  built-in constant

That ordering is the whole trick. Nothing in the pipeline branches on "is this the
owner?"; each number just takes the best source available for it.

Pure stdlib and dependency-free, like `registry`, because it is copied into both
container images.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .registry import BLOCKS, DAILY_COLUMNS, Column

# The blocks a user can actually toggle. `key` (the date) and `meta` (bookkeeping)
# are structural — every row has them whatever the user owns.
STRUCTURAL_BLOCKS: FrozenSet[str] = frozenset({"key", "meta"})
TOGGLEABLE_BLOCKS: Tuple[str, ...] = tuple(
    b for b in BLOCKS if b not in STRUCTURAL_BLOCKS)

# Named block sets. Sugar over `blocks`, never a rank: a preset is only a
# shorthand for the set it expands to, and any explicit set is equally valid.
PRESETS: Dict[str, Tuple[str, ...]] = {
    # A phone and nothing else. `self_report` rides along because a bowel note
    # needs no hardware — just the same text Shortcut the meals use.
    "nutrition": ("nutrition", "self_report"),
    # + a wearable that reports sleep and overnight recovery.
    "nutrition_sleep": ("nutrition", "self_report", "sleep", "recovery"),
    # + the activity side of the same wearable.
    "nutrition_activity": ("nutrition", "self_report", "activity"),
    # Everything this schema knows how to hold.
    "full": TOGGLEABLE_BLOCKS,
}
DEFAULT_PRESET = "full"

# The coach domain each block belongs to. Several blocks fold into one domain
# because they are one subject to a reader: sleep and overnight recovery are both
# "how you slept", and a card that separated them would be pedantry.
BLOCK_DOMAINS: Dict[str, str] = {
    "nutrition": "nutrition",
    "sleep": "sleep",
    "recovery": "sleep",
    "activity": "activity",
    "body": "body",
    "self_report": "digestion",
}

# What a domain is called in the user's language, for the prompt and the cards.
DOMAIN_LABELS_PT: Dict[str, str] = {
    "nutrition": "alimentação",
    "sleep": "sono e recuperação",
    "activity": "atividade e treino",
    "body": "composição corporal",
    "digestion": "digestão",
}

# Goals. This is a SEPARATE axis from capability: what you own decides what can be
# observed, the goal decides what the advice is for. Conflating them is why the old
# ProfileView had "Recomposição" hard-coded for everyone.
GOALS: Tuple[str, ...] = (
    "recomposition",    # lose fat, hold muscle (the owner's)
    "fat_loss",
    "muscle_gain",
    "maintenance",
    "health",           # no body-composition goal; eat well, feel well
)
DEFAULT_GOAL = "recomposition"

GOAL_LABELS_PT: Dict[str, str] = {
    "recomposition": "recomposição corporal — perder gordura mantendo músculo",
    "fat_loss": "perda de gordura",
    "muscle_gain": "ganho de massa muscular",
    "maintenance": "manutenção do peso e da composição atual",
    "health": "comer melhor e sentir-se bem, sem objetivo de peso",
}

# Declared activity levels -> the multiplier applied to a computed BMR when there
# is no measured expenditure to use instead. Standard Harris-Benedict/Mifflin
# activity factors.
ACTIVITY_FACTORS: Dict[str, float] = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}
DEFAULT_ACTIVITY_LEVEL = "moderate"

SEXES: Tuple[str, ...] = ("male", "female")


def _clean_blocks(raw: Iterable[str]) -> Tuple[str, ...]:
    """Keep declaration order (so downstream output is stable), drop unknowns and
    duplicates. An unknown block name is ignored rather than fatal: this is
    hand-edited configuration in a spreadsheet, and one typo must not take the
    service down."""
    asked = {str(b).strip().lower() for b in raw}
    return tuple(b for b in TOGGLEABLE_BLOCKS if b in asked)


@dataclass(frozen=True)
class Capabilities:
    """One user's switch: what they measure, what they are aiming at, and what they
    have simply told us about themselves."""

    blocks: Tuple[str, ...] = TOGGLEABLE_BLOCKS
    goal: str = DEFAULT_GOAL
    # Declared, not measured. Every one is optional — an empty profile just means
    # the built-in constants stay in charge.
    sex: Optional[str] = None
    age: Optional[int] = None
    height_cm: Optional[float] = None
    declared_weight_kg: Optional[float] = None
    activity_level: str = DEFAULT_ACTIVITY_LEVEL
    # Which preset name (if any) produced `blocks`, purely so the UI can show it.
    preset: Optional[str] = None

    # -- what is on ------------------------------------------------------------
    def has(self, block: str) -> bool:
        return block in self.blocks

    def has_all(self, *blocks: str) -> bool:
        return all(b in self.blocks for b in blocks)

    def has_any(self, *blocks: str) -> bool:
        return any(b in self.blocks for b in blocks)

    def visible_blocks(self) -> Tuple[str, ...]:
        """The blocks an API response may carry, structural ones included."""
        return tuple(b for b in BLOCKS
                     if b in STRUCTURAL_BLOCKS or b in self.blocks)

    def columns(self) -> List[Column]:
        """Every column this user can ever have a value for."""
        return [c for c in DAILY_COLUMNS
                if c.block in STRUCTURAL_BLOCKS or c.block in self.blocks]

    def column_names(self) -> FrozenSet[str]:
        return frozenset(c.name for c in self.columns())

    def sources(self) -> FrozenSet[str]:
        """The integrations worth running for this user. `derived` and `system`
        are dropped: they are computed here, not fetched from anywhere."""
        return frozenset(c.source for c in self.columns()
                         if c.source not in ("derived", "system"))

    def needs_google_health(self) -> bool:
        """Whether the daily job has anything at all to pull. False for a
        nutrition-only user, which is what makes onboarding a friend an
        ingest-service-only affair — no OAuth, no scheduled job."""
        return "fitbit" in self.sources()

    # -- coach ----------------------------------------------------------------
    def domains(self) -> Tuple[str, ...]:
        """The coach domains that may produce findings, in a stable order."""
        seen: List[str] = []
        for block in TOGGLEABLE_BLOCKS:
            domain = BLOCK_DOMAINS.get(block)
            if block in self.blocks and domain and domain not in seen:
                seen.append(domain)
        return tuple(seen)

    def blind_spots(self) -> Tuple[str, ...]:
        """The domains this user has NO data for. The coach is told these
        explicitly: a model given no sleep data still writes confident sleep
        advice unless it is told the data does not exist."""
        live = set(self.domains())
        seen: List[str] = []
        for block in TOGGLEABLE_BLOCKS:
            domain = BLOCK_DOMAINS.get(block)
            if domain and domain not in live and domain not in seen:
                seen.append(domain)
        return tuple(seen)

    def can_link(self, *blocks: str) -> bool:
        """Whether a cross-domain hypothesis is evaluable at all. A link needs
        every block it spans, so a nutrition-only user gets none — not a silenced
        one, an absent one."""
        return self.has_all(*blocks)

    # -- the declared body -----------------------------------------------------
    def basal_metabolic_rate(self) -> Optional[float]:
        """Mifflin-St Jeor from the DECLARED profile, or None if it is incomplete.

        Only ever a fallback. A measured `bmr_kcal` off the scale, and better still
        a measured `total_cals_out`, beat this every time — this exists so someone
        with no hardware still gets a target that describes a body roughly their
        size instead of a hard-coded 2200.
        """
        if self.age is None or self.height_cm is None \
                or self.declared_weight_kg is None or self.sex not in SEXES:
            return None
        base = (10 * self.declared_weight_kg) + (6.25 * self.height_cm) \
            - (5 * self.age)
        return base + (5 if self.sex == "male" else -161)

    def declared_tdee(self) -> Optional[float]:
        """Declared BMR scaled by the declared activity level."""
        bmr = self.basal_metabolic_rate()
        if bmr is None:
            return None
        factor = ACTIVITY_FACTORS.get(self.activity_level,
                                      ACTIVITY_FACTORS[DEFAULT_ACTIVITY_LEVEL])
        return bmr * factor

    # -- serialisation ---------------------------------------------------------
    def to_api(self) -> Dict[str, Any]:
        """What the app is told. The app renders from this rather than from a
        build-time constant, so switching a block on in the sheet changes the UI on
        the next fetch with no release."""
        return {
            "preset": self.preset,
            "blocks": list(self.blocks),
            "domains": list(self.domains()),
            "blind_spots": list(self.blind_spots()),
            "goal": self.goal,
            "goal_label_pt": GOAL_LABELS_PT.get(self.goal, self.goal),
            "declared": {
                "sex": self.sex,
                "age": self.age,
                "height_cm": self.height_cm,
                "weight_kg": self.declared_weight_kg,
                "activity_level": self.activity_level,
            },
        }


# The owner's setup, and the default for any sheet with no `config` tab yet — so
# nothing changes for an existing deployment until someone edits the sheet.
FULL = Capabilities(blocks=TOGGLEABLE_BLOCKS, preset="full")


def from_preset(name: str, **overrides: Any) -> Capabilities:
    blocks = PRESETS.get(name, PRESETS[DEFAULT_PRESET])
    return Capabilities(blocks=_clean_blocks(blocks), preset=name, **overrides)


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return out


def from_config(rows: Sequence[Dict[str, Any]]) -> Capabilities:
    """Build a capability from the sheet's `config` tab (`key`/`value` rows).

    Tolerant by construction. This is a spreadsheet a human edits, so every field
    falls back to the default rather than raising: the worst a typo can do is leave
    that one setting at its default, never take the service down or — much worse —
    silently hide a block the user does have.

    `blocks` accepts either a preset name (`full`, `nutrition`) or an explicit
    comma-separated list (`nutrition, sleep, recovery`).
    """
    values: Dict[str, str] = {}
    for row in rows or ():
        key = str(row.get("key") or "").strip().lower()
        if key:
            values[key] = str(row.get("value") or "").strip()

    raw_blocks = values.get("blocks", "").strip()
    preset: Optional[str] = None
    if not raw_blocks:
        preset, blocks = DEFAULT_PRESET, PRESETS[DEFAULT_PRESET]
    elif raw_blocks.lower() in PRESETS:
        preset = raw_blocks.lower()
        blocks = PRESETS[preset]
    else:
        blocks = tuple(b for b in raw_blocks.replace(";", ",").split(","))
    cleaned = _clean_blocks(blocks)
    if not cleaned:
        # An explicit list that matched nothing is a typo, not a request to turn
        # the whole app off. Nutrition is the one block every user has.
        preset, cleaned = "nutrition", _clean_blocks(PRESETS["nutrition"])

    goal = values.get("goal", "").strip().lower() or DEFAULT_GOAL
    if goal not in GOALS:
        goal = DEFAULT_GOAL

    sex = values.get("sex", "").strip().lower() or None
    if sex not in SEXES:
        sex = None

    age = _num(values.get("age"))
    activity = values.get("activity_level", "").strip().lower()
    if activity not in ACTIVITY_FACTORS:
        activity = DEFAULT_ACTIVITY_LEVEL

    return Capabilities(
        blocks=cleaned,
        goal=goal,
        sex=sex,
        age=int(age) if age is not None else None,
        height_cm=_num(values.get("height_cm")),
        declared_weight_kg=_num(values.get("weight_kg")),
        activity_level=activity,
        preset=preset,
    )


# The rows written into a fresh `config` tab: the current behaviour, spelled out,
# so the first thing a user sees is an editable description of what they have
# rather than an empty grid.
CONFIG_TAB_HEADERS: Tuple[str, ...] = ("key", "value", "notes")

CONFIG_SEED: Tuple[Tuple[str, str, str], ...] = (
    ("blocks", DEFAULT_PRESET,
     "full | nutrition | nutrition_sleep | nutrition_activity, or an explicit "
     "list: nutrition, sleep, recovery, activity, body, self_report"),
    ("goal", DEFAULT_GOAL, " | ".join(GOALS)),
    ("sex", "", "male | female — only used when there is no measured body data"),
    ("age", "", "years — only used when there is no measured body data"),
    ("height_cm", "", "only used when there is no measured body data"),
    ("weight_kg", "", "declared weight; a scale reading always wins over this"),
    ("activity_level", DEFAULT_ACTIVITY_LEVEL, " | ".join(ACTIVITY_FACTORS)),
)
