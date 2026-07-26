"""The things that happened, as opposed to the things that are always true.

`food_patterns` answers "how does this person eat" — averages, streaks, servings a
week. That is the right lens for habits and the wrong one for a Friday night. Eight
drinks at a party disappears into a weekly average; a Big Tasty menu reads as
"refined grain, red meat, fried potato" and loses the only fact that matters, which
is that the user went to McDonald's and knows it.

This module finds the *occasions* — and, just as importantly, reads what the user
wrote about them. The meal log carries a free-text note in the user's own words
("Comi um menu médio Big Tasty do McDonalds", "Bebi uns 100ml de vinho branco") that
until now never reached the model at all. A coach that can't see the note is guessing
at context the user already supplied.

Each event carries an `importance` in 0..1, which is what lets `coach_recall` rank a
night out above a Tuesday breakfast when it decides what to remember months later —
the same role importance plays in the Generative Agents memory stream.

Everything here is deterministic. The model is told "six drinks between 22:10 and
01:40, on a Friday, plus a burger at 02:00"; the judgement about what that means, and
the tone to take about it, is the model's.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

import food_taxonomy as tax
from food_patterns import SLOT_LABELS

# Drinks in one day. Two is a normal evening; four-plus is an occasion, and the coach
# should read it as one rather than as a nutrition failure.
DRINKS_NOTABLE = 2.0
DRINKS_HEAVY = 4.0

# A day this far over the calorie ceiling is an event in itself, whatever it was made
# of.
SURPLUS_RATIO = 1.25
DEFICIT_RATIO = 0.7

# Brands and phrases that mean "this was eaten out", read from the raw food names AND
# from the user's note. The note is often the only place the occasion appears: the
# items say "burger, fries, iced tea" and the note says which restaurant.
_EATING_OUT_MARKERS = (
    "mcdonald", "mcmenu", "big tasty", "big mac", "mcchicken", "cheeseburger",
    "burger king", "whopper", "kfc", "telepizza", "pizza hut", "dominos",
    "domino's", "subway", "h3", "vitaminas", "padaria portuguesa", "starbucks",
    "take away", "takeaway", "take-away", "uber eats", "ubereats", "glovo",
    "bolt food", "delivery", "restaurante", "tasca", "churrasqueira", "sushi",
    "fast food", "menu medio", "menu grande", "drive",
)

# Phrases in a note that signal an occasion rather than a meal.
_OCCASION_MARKERS = (
    "festa", "aniversario", "jantar fora", "almoco fora", "convivio", "party",
    "casamento", "boda", "ceia", "saida", "bar", "discoteca", "copos", "petiscos",
    "celebrar", "comemorar", "ferias", "viagem", "restaurante", "com amigos",
    "com a familia", "trabalho", "reuniao", "cinema",
)

# Phrases that say the user already knows and is telling you the context. These make a
# note worth quoting back rather than lecturing over.
_SELF_AWARE_MARKERS = (
    "exagerei", "sei que", "fugi", "escorreguei", "cheat", "excecao", "excecional",
    "so hoje", "nao resisti", "estava com fome", "nao tive tempo", "unica vez",
)


def _fold(text: Any) -> str:
    """Lowercase, accent-free, whitespace-collapsed — so "Comi um Menu Médio" matches
    "menu medio"."""
    raw = "".join(c for c in unicodedata.normalize("NFD", str(text or ""))
                  if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", raw).strip()


def _any_marker(text: str, markers: Sequence[str]) -> List[str]:
    return [m for m in markers if m in text]


def _hhmm(datetime_str: str) -> str:
    return str(datetime_str)[11:16]


def _event(kind: str, *, day: str, importance: float, headline: str,
           detail: str = "", topics: Sequence[str] = (),
           evidence: Optional[Dict[str, Any]] = None,
           at: str = "") -> Dict[str, Any]:
    return {
        "kind": kind,
        "date": day,
        "at": at,
        "importance": round(min(max(importance, 0.0), 1.0), 2),
        "headline": headline,
        "detail": detail,
        # Topics are how `coach_recall` finds this again: exact keys, no embeddings.
        "topics": sorted({t for t in topics if t}),
        "evidence": evidence or {},
    }


def _weekday_pt(day: str) -> str:
    names = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo"]
    try:
        return names[date.fromisoformat(day).weekday()]
    except (ValueError, IndexError):
        return ""


def notes_for(meals: Sequence[Dict[str, Any]],
              rows: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """The user's own words per meal, keyed by the meal's datetime.

    `food_patterns.read_meals` drops the note (it reasons about foods), so it is
    picked back up here from the raw rows.
    """
    out: Dict[str, str] = {}
    for row in rows:
        note = " ".join(str(row.get("note") or "").split())
        when = str(row.get("datetime") or "")
        if note and when:
            out[when] = note[:400]
    return out


def detect(meals: Sequence[Dict[str, Any]], *, day: str,
           notes: Optional[Dict[str, str]] = None,
           calories: float = 0.0,
           calorie_ceiling: float = 0.0) -> List[Dict[str, Any]]:
    """Everything notable about one day, most important first."""
    notes = notes or {}
    todays = [m for m in meals if m.get("date") == day]
    if not todays:
        return []

    events: List[Dict[str, Any]] = []
    weekday = _weekday_pt(day)

    # -- drinking ---------------------------------------------------------------
    drinks = [(m, i) for m in todays for i in m["items"]
              if i["group"] == "alcohol"]
    if drinks:
        # Counted as DRINKS, not as gram-servings. A reference serving of "alcohol"
        # is meaningless across the category — a 40 ml shot of vodka is a sixth of a
        # 330 ml beer by weight and the same thing socially — so eight shots would
        # have scored as one and a bit "servings" and a night out would have
        # registered as a quiet evening. One logged alcoholic item is one drink.
        servings = float(len(drinks))
        times = sorted(_hhmm(m["datetime"]) for m, _i in drinks)
        what = sorted({i["food"] for _m, i in drinks})
        if servings >= DRINKS_HEAVY:
            importance, kind = 0.9, "drinking_occasion"
            headline = (f"{servings:g} bebidas alcoólicas entre {times[0]} e "
                        f"{times[-1]}, {weekday}")
        elif servings >= DRINKS_NOTABLE:
            importance, kind = 0.6, "drinking"
            headline = f"{servings:g} bebidas alcoólicas, {weekday}"
        else:
            importance, kind = 0.3, "drinking"
            headline = f"uma bebida alcoólica ({', '.join(what)}), {weekday}"
        events.append(_event(
            kind, day=day, importance=importance, headline=headline,
            detail=", ".join(what), topics=["alcohol", *what], at=times[0],
            evidence={"drinks": servings, "what": what, "first": times[0],
                      "last": times[-1], "weekday": weekday,
                      "grams": round(sum(i["grams"] for _m, i in drinks))}))

    # -- eaten out / fast food ---------------------------------------------------
    # Searched across food names AND the note, because the note is usually where the
    # restaurant actually appears.
    haystacks = []
    for meal in todays:
        text = _fold(" ".join(i["raw"] for i in meal["items"]))
        note = _fold(notes.get(meal["datetime"], ""))
        haystacks.append((meal, f"{text} {note}".strip(), note))
    for meal, text, note in haystacks:
        markers = _any_marker(text, _EATING_OUT_MARKERS)
        if not markers:
            continue
        slot = SLOT_LABELS.get(meal["slot"], meal["slot"])
        events.append(_event(
            "eaten_out", day=day, importance=0.7,
            headline=(f"{markers[0]} ao {slot} — "
                      f"{round(meal['calories'])} kcal numa refeição"),
            detail=notes.get(meal["datetime"], "") or ", ".join(
                i["food"] for i in meal["items"]),
            topics=["eaten_out", *markers, *(i["group"] for i in meal["items"])],
            at=_hhmm(meal["datetime"]),
            evidence={"markers": markers, "calories": round(meal["calories"]),
                      "protein_g": round(meal["protein_g"], 1),
                      "slot": meal["slot"],
                      "foods": [i["food"] for i in meal["items"]],
                      "note": notes.get(meal["datetime"], "")}))

    # -- the day's size ----------------------------------------------------------
    if calorie_ceiling > 0 and calories > 0:
        ratio = calories / calorie_ceiling
        if ratio >= SURPLUS_RATIO:
            events.append(_event(
                "big_day", day=day, importance=0.6,
                headline=(f"{round(calories)} kcal, {round((ratio - 1) * 100)}% acima "
                          f"do teto de {round(calorie_ceiling)}"),
                topics=["calories", "surplus"],
                evidence={"calories": round(calories),
                          "ceiling": round(calorie_ceiling),
                          "ratio": round(ratio, 2)}))
        elif ratio <= DEFICIT_RATIO:
            events.append(_event(
                "light_day", day=day, importance=0.4,
                headline=f"só {round(calories)} kcal no dia todo",
                topics=["calories", "deficit"],
                evidence={"calories": round(calories),
                          "ceiling": round(calorie_ceiling)}))

    # -- what the user said about it ---------------------------------------------
    for meal, text, note in haystacks:
        if not note:
            continue
        occasions = _any_marker(note, _OCCASION_MARKERS)
        aware = _any_marker(note, _SELF_AWARE_MARKERS)
        if not occasions and not aware:
            continue
        events.append(_event(
            "noted", day=day,
            # Someone telling you they already know they overdid it is worth more than
            # any inference: it changes the right thing to say, not just the facts.
            importance=0.75 if aware else 0.5,
            headline=f"nota tua ao {SLOT_LABELS.get(meal['slot'], meal['slot'])}",
            detail=notes.get(meal["datetime"], ""),
            topics=["note", *occasions, *(["self_aware"] if aware else [])],
            at=_hhmm(meal["datetime"]),
            evidence={"note": notes.get(meal["datetime"], ""),
                      "occasion_markers": occasions,
                      "self_aware": bool(aware)}))

    events.sort(key=lambda e: (-e["importance"], e["at"]))
    return events


def for_prompt(events: Sequence[Dict[str, Any]], limit: int = 6
               ) -> List[Dict[str, Any]]:
    """Events as the model should see them: the fact, when, and the user's own words
    where there are any. The bookkeeping stays here."""
    out = []
    for event in events[:limit]:
        entry = {"what": event["headline"], "when": event["at"] or event["date"]}
        if event.get("detail"):
            entry["context"] = event["detail"]
        out.append(entry)
    return out


def day_importance(events: Sequence[Dict[str, Any]]) -> float:
    """How much this day is worth remembering. Feeds the archive's retrieval ranking
    so that months later "the Friday with eight drinks" outranks a normal Tuesday."""
    if not events:
        return 0.1
    top = max(e["importance"] for e in events)
    # A day with several notable things is more memorable than the sum of its parts,
    # but never more memorable than a maximally notable one.
    return round(min(1.0, top + 0.05 * (len(events) - 1)), 2)
