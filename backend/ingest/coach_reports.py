"""Weekly, monthly and yearly reviews — the consolidated tier.

Each level reads only the level below:

    weekly   <- the week's meals in full, its cards, its conversations, its events,
                the deterministic weekly stats, and last week's report
    monthly  <- four or five weekly reports + the month's deterministic stats
    yearly   <- twelve monthly reports

That is what makes a yearly report possible at all. Reading a year of meals would be
tens of thousands of lines and a prompt no model handles well; reading twelve monthly
summaries is a page. The cost of writing any report stays flat as the history grows,
which is the property the whole archive exists to provide.

Reports are written by the strongest model available (Opus at medium effort on the
Mac) because this is the one job where the reasoning is genuinely hard: correlating
what was advised against what was eaten, across weeks, and saying something true about
the direction of travel. The daily feed is a different job — fast, frequent, cheap —
and stays on Sonnet.

Everything here is prompt construction and deterministic fact-gathering. As everywhere
else in the coach, the model writes sentences; the arithmetic is done in code and
handed to it.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

PERIODS = ("weekly", "monthly", "yearly")


# -- period arithmetic ---------------------------------------------------------

def week_key(day: date) -> str:
    """The Monday of the week `day` falls in — the key a weekly report is stored
    under."""
    return (day - timedelta(days=day.weekday())).isoformat()


def period_bounds(period: str, ref: date) -> tuple:
    """(start, end, key) for the period that just COMPLETED before `ref`.

    Reports are always written about a finished stretch of time. A "weekly review" of
    a week still in progress would be revised by Sunday dinner and is not a review.
    """
    if period == "weekly":
        end = ref - timedelta(days=ref.weekday() + 1)     # last Sunday
        start = end - timedelta(days=6)
        return start.isoformat(), end.isoformat(), week_key(start)
    if period == "monthly":
        first_this = ref.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return start.isoformat(), end.isoformat(), start.isoformat()[:7]
    if period == "yearly":
        year = ref.year - 1
        return f"{year}-01-01", f"{year}-12-31", str(year)
    raise ValueError(f"unknown period {period!r}")


# -- the facts a report reasons over -------------------------------------------

def weekly_facts(*, start: str, end: str, key: str,
                 meals: Sequence[Dict[str, Any]],
                 profile: Dict[str, Any],
                 archive_entries: Sequence[Dict[str, Any]],
                 previous: Optional[Dict[str, Any]] = None,
                 memory_facts: Sequence[Dict[str, Any]] = (),
                 metric_findings: Sequence[Dict[str, Any]] = (),
                 days: Sequence[Dict[str, Any]] = (),
                 capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A week, in full: every meal with its items, everything the coach said, every
    conversation, and the deterministic group statistics.

    The week is small enough to include whole — seven days is perhaps twenty meals —
    and that completeness is the point. This is the one moment the model gets to see
    the raw material rather than a summary of it.

    **This is also where breadth belongs.** A daily card is one idea about one
    domain, because a card that tries to cover food, sleep and training at once
    covers none of them. The week is the horizon that can afford the whole picture —
    it runs once, the user reads all of it, and connecting the domains is precisely
    what makes it worth reading. So `metric_findings` (every non-food finding and
    every measured link, not just the one or two a daily feed had room for) and the
    week's daily rows arrive here unbudgeted.
    """
    cards = [e for e in archive_entries if e.get("kind") == "card"]
    chats = [e for e in archive_entries if e.get("kind") == "chat"]
    events = [e for e in archive_entries if e.get("kind") == "event"]

    return {
        "period": "weekly",
        "covering": {"from": start, "to": end, "key": key},
        "meals": [
            {"date": meal["date"], "time": meal["datetime"][11:16],
             "slot": meal["slot"], "calories": round(meal["calories"]),
             "protein_g": round(meal["protein_g"], 1),
             "note": meal.get("note", ""),
             # pt-PT: the report this feeds is written in Portuguese and quotes
             # these names back verbatim.
             "items": [{"food": i.get("pt") or i["food"],
                        "grams": round(i["grams"]),
                        "group": i["group"]} for i in meal["items"]]}
            for meal in meals
        ],
        "food_patterns": {
            "days_logged": profile.get("days_logged"),
            "groups": {k: {"servings_per_week": v.get("servings_per_week"),
                           "reference_min": v.get("week_min"),
                           "reference_max": v.get("week_max"),
                           "top_foods": v.get("top_foods_pt", [])}
                       for k, v in (profile.get("groups") or {}).items()},
            "variety": profile.get("variety"),
            "meal_slots": profile.get("slots"),
            "streaks": [{**s, "food": s.get("pt") or s["food"]}
                        for s in profile.get("streaks", [])[:5]],
            "findings": [{"fact": f["headline"], "severity": f["severity"]}
                         for f in profile.get("findings", [])[:8]],
        },
        # Sleep, training, body composition, digestion — and the measured links
        # between them and the food above. The daily feed sees one of these at a
        # time; the review sees all of them, which is the only place the chains
        # across a whole week can actually be read.
        "the_rest_of_your_week": [
            {"domain": f.get("domain"), "fact": f["headline"],
             "evidence": f.get("evidence", {}), "severity": f.get("severity")}
            for f in metric_findings
        ] or None,
        "daily_metrics": [
            {k: v for k, v in day.items() if v not in (None, "")}
            for day in days
        ] or None,
        "capabilities": capabilities,
        # What the coach actually said, day by day. The week's advice is as much a
        # subject of the review as the week's food: did it land, was it acted on, was
        # it the right thing to have said?
        "what_you_were_told": [
            {"date": c["date"], "title": c["summary"], "said": c.get("body", ""),
             "kind": (c.get("data") or {}).get("kind"),
             "swap": (c.get("data") or {}).get("swap")}
            for c in cards
        ],
        "conversations": [
            {"date": c["date"], "you_asked": (c.get("data") or {}).get("question"),
             "answer": (c.get("data") or {}).get("answer", "")[:400]}
            for c in chats
        ],
        "notable_events": [
            {"date": e["date"], "what": e["summary"], "context": e.get("body", ""),
             "importance": e.get("importance")}
            for e in sorted(events, key=lambda e: -float(e.get("importance") or 0))[:12]
        ],
        "about_you": list(memory_facts)[:20],
        "last_week": ({"headline": previous.get("headline"),
                       "summary": previous.get("summary"),
                       "focus": previous.get("focus")} if previous else None),
    }


def rollup_facts(*, period: str, start: str, end: str, key: str,
                 children: Sequence[Dict[str, Any]],
                 profile: Optional[Dict[str, Any]] = None,
                 memory_facts: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """A monthly or yearly report's inputs: the reports one level down, and nothing
    else. No meals, no cards — those were already read and summarised by the level
    below, and reading them again is what would make this unaffordable."""
    return {
        "period": period,
        "covering": {"from": start, "to": end, "key": key},
        "made_of": [
            {"covering": child.get("key"), "headline": child.get("headline"),
             "summary": child.get("summary"), "focus": child.get("focus"),
             "wins": child.get("wins", []), "numbers": child.get("numbers", {})}
            for child in children
        ],
        "current_habits": ({
            "groups": {k: {"servings_per_week": v.get("servings_per_week"),
                           "reference_min": v.get("week_min"),
                           "reference_max": v.get("week_max")}
                       for k, v in (profile.get("groups") or {}).items()},
            "variety": profile.get("variety"),
        } if profile else None),
        "about_you": list(memory_facts)[:20],
    }


# -- the prompts ---------------------------------------------------------------

_REPORT_VOICE = """És o nutricionista pessoal desta pessoa há meses. Português de
Portugal, tratamento por "tu". O objetivo dela é recomposição corporal: perder gordura
mantendo músculo, com proteína alta.

Isto não é um cartão diário — é a revisão que se faz sentado, com tempo. Espera-se
profundidade, não um resumo simpático.

O QUE FAZ UMA BOA REVISÃO:
- Liga o que foi DITO ao que foi COMIDO. Tens `what_you_were_told` (tudo o que lhe
  disseste no período) e a comida toda. A pergunta central é: o conselho pegou? A
  troca que sugeriste apareceu no prato? Se sim, diz e dá crédito. Se não, não repitas
  o conselho mais alto — pergunta-te porquê e propõe algo diferente.
- Vê a DIREÇÃO, não só o estado. Com `last_week` (ou os períodos anteriores) podes
  dizer se algo está a melhorar, a piorar ou apenas a oscilar. É a única coisa que um
  cartão diário nunca consegue dizer.
- Trata os ACONTECIMENTOS como acontecimentos. Uma sexta-feira com bebidas e um
  hambúrguer às duas da manhã não é "um dia acima do teto": é uma noite de convívio.
  Diz o que dirias a um amigo — sem drama, sem sermão, com o que fazer a seguir.
- Usa as CONVERSAS. O que a pessoa te perguntou diz-te o que lhe interessa.
- Números só amarrados a comida concreta. Nada de "aumenta a fibra".

REGRAS: não inventes alimentos nem números que não estejam nos FACTOS; nada de
linguagem médica; enquadra sempre como acrescentar ou trocar, nunca como restringir
por restringir; sem emojis; sem jargão."""

_WEEKLY_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "headline": "uma frase que resume a semana — concreta, não genérica",
  "summary": "3 a 6 frases: o que aconteceu, o que mudou face à semana anterior, e o que isso significa",
  "meal_reviews": [
    {"what": "a refeição ou o padrão", "verdict": "good|mixed|poor",
     "why": "uma frase concreta"}
  ],
  "wins": [{"title": "curto", "detail": "uma frase"}],
  "focus": {"label": "a UMA coisa para a próxima semana",
            "why": "porquê, ligado a comida concreta",
            "how": "o primeiro passo, prático"},
  "advice_review": {"landed": ["conselhos que pegaram"],
                    "ignored": ["conselhos que não pegaram"],
                    "note": "uma frase honesta sobre isso"},
  "events": [{"what": "o acontecimento", "take": "o que dirias sobre ele"}],
  "numbers": {"chave": "valor — só números que estejam nos FACTOS"}
}"""

_ROLLUP_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "headline": "uma frase sobre o período inteiro",
  "summary": "4 a 8 frases: a trajetória do período, o que mudou de facto, o que ficou igual",
  "arc": [{"when": "a sub-parte (semana ou mês)", "what": "o que a caracterizou"}],
  "wins": [{"title": "curto", "detail": "uma frase"}],
  "focus": {"label": "a UMA coisa para o período seguinte", "why": "...", "how": "..."},
  "numbers": {"chave": "valor"}
}"""


def build_prompt(facts: Dict[str, Any]) -> str:
    period = facts.get("period", "weekly")
    schema = _WEEKLY_SCHEMA if period == "weekly" else _ROLLUP_SCHEMA
    horizon = {"weekly": "uma SEMANA", "monthly": "um MÊS",
               "yearly": "um ANO"}.get(period, period)
    return (f"{_REPORT_VOICE}\n\nEstás a rever {horizon}.\n\nFACTOS (JSON, já "
            f"calculados):\n{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}"
            f"\n\n{schema}\n\nResponde com o JSON na tua resposta, e mais nada.")


def assemble_report(answer: Dict[str, Any], *, period: str, key: str,
                    start: str, end: str, now: datetime,
                    source: str) -> Dict[str, Any]:
    """Validate a model's report into the stored shape. Free text is clipped, lists
    are bounded, and anything missing simply isn't there — a thin report is better
    than a padded one."""
    def clip(value: Any, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit]

    def items(value: Any, limit: int, mapper) -> List[Dict[str, Any]]:
        return [mapper(v) for v in (value or [])[:limit] if isinstance(v, dict)]

    report = {
        "period": period,
        "key": key,
        "covering": {"from": start, "to": end},
        "generated_at": now.isoformat(timespec="seconds"),
        "source": source,
        "headline": clip(answer.get("headline"), 200),
        "summary": clip(answer.get("summary"), 2000),
        "wins": items(answer.get("wins"), 5,
                      lambda w: {"title": clip(w.get("title"), 80),
                                 "detail": clip(w.get("detail"), 240)}),
        "numbers": {clip(k, 40): clip(v, 80)
                    for k, v in (answer.get("numbers") or {}).items()
                    if isinstance(answer.get("numbers"), dict)},
    }
    focus = answer.get("focus")
    if isinstance(focus, dict):
        report["focus"] = {"label": clip(focus.get("label"), 120),
                           "why": clip(focus.get("why"), 300),
                           "how": clip(focus.get("how"), 300)}
    if period == "weekly":
        report["meal_reviews"] = items(
            answer.get("meal_reviews"), 8,
            lambda m: {"what": clip(m.get("what"), 120),
                       "verdict": clip(m.get("verdict"), 12),
                       "why": clip(m.get("why"), 240)})
        review = answer.get("advice_review")
        if isinstance(review, dict):
            report["advice_review"] = {
                "landed": [clip(x, 160) for x in (review.get("landed") or [])[:5]],
                "ignored": [clip(x, 160) for x in (review.get("ignored") or [])[:5]],
                "note": clip(review.get("note"), 300)}
        report["events"] = items(
            answer.get("events"), 6,
            lambda e: {"what": clip(e.get("what"), 160),
                       "take": clip(e.get("take"), 300)})
    else:
        report["arc"] = items(answer.get("arc"), 12,
                              lambda a: {"when": clip(a.get("when"), 40),
                                         "what": clip(a.get("what"), 240)})
    return report


def as_card_fields(report: Dict[str, Any]) -> Dict[str, Any]:
    """The report reduced to a feed card. The card is the doorway; the full report
    lives in the archive and is read in the app's history."""
    period = report.get("period")
    label = {"weekly": "A tua semana", "monthly": "O teu mês",
             "yearly": "O teu ano"}.get(period, "Revisão")
    kind = {"weekly": "weekly_review", "monthly": "monthly_review",
            "yearly": "yearly_review"}.get(period, "weekly_review")
    body = report.get("summary") or ""
    focus = report.get("focus") or {}
    if focus.get("label"):
        body = f"{body} Foco: {focus['label']}."
    return {"kind": kind,
            "title": report.get("headline") or label,
            "body": body.strip(),
            "chips": [{"label": f"{label} · {report.get('key')}", "tone": "neutral"}]}
