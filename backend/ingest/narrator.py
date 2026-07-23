"""Gemini-powered narration for the Coach feature.

Replaces the local `claude` CLI with direct Gemini API calls. The deterministic
analysis already happened in `insights.py` — this module only turns finished facts
into human coaching prose. Can run in the backend (Cloud Run) or locally.

Three generation modes, all called as top-level functions:

  narrate_weekly(diagnosis, profile, continuity, ...)
      -> { headline, wins, focus, swap, continuity, encouragement }

  assemble_next_meal(context, profile, ...)
      -> { next_slot, plates: [{ title, items, covers, ... }] }

    where `context` already contains today's remaining budget, shortfalls, the
    user's meal-timing profile, and per-shortfall candidate foods with portion
    ranges. The model picks the next slot AND assembles 3 plates for it.

  critic_pass(diagnosis, draft)
      -> { ok, issues, report } — validates a weekly report against the facts.

Design invariants carried from Phase 2:
  * The model NEVER invents a number. Every numerical claim in the output must be
    traceable to the facts in the prompt.
  * The critic pass rejects any claim the facts don't support, any alarm the
    policy says is benign, any restrictive framing.
  * `response_mime_type` is NOT relied on — Gemini's structured-output mode is
    less flexible than our prompt-engineered JSON extraction. We ask for JSON
    in the prompt and extract it from the text response, matching the robust
    parsing pattern the earlier claude_cli module proved in production.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("narrator")

# -- config --------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_NARRATOR_MODEL", "gemini-2.0-flash")
GEMINI_CRITIC_MODEL = os.environ.get("GEMINI_CRITIC_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "90"))


# -- Gemini API transport ------------------------------------------------------

class GeminiError(RuntimeError):
    """Any transport/parse failure. Callers catch this and degrade gracefully."""


def call_gemini(prompt: str, *, require_key: str = "headline",
                api_key: Optional[str] = None,
                model: Optional[str] = None,
                timeout_s: Optional[int] = None,
                temperature: float = 0.2) -> Dict[str, Any]:
    """Call the Gemini API and return a JSON dict containing `require_key`.

    Args:
        prompt: The full system+user prompt string.
        require_key: The JSON key that must be present in the response object.
        api_key: Override for GEMINI_API_KEY env var.
        model: Override for GEMINI_NARRATOR_MODEL env var.
        timeout_s: Request timeout in seconds.
        temperature: Generation temperature (0.0 = deterministic, 1.0 = creative).

    Raises:
        GeminiError on any transport/parse failure.
    """
    key = api_key or GEMINI_API_KEY
    if not key:
        raise GeminiError("GEMINI_API_KEY is not configured")
    model_id = model or GEMINI_MODEL
    to = timeout_s or GEMINI_TIMEOUT_S

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_id}:generateContent?key={key}")

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "topP": 0.95,
            "maxOutputTokens": 8192,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }

    raw = None
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=to) as resp:
            raw = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise GeminiError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GeminiError(f"Gemini transport error: {exc}") from exc

    # Extract the response text.
    try:
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiError(f"Unexpected Gemini response shape: {json.dumps(raw, indent=1)[:600]}") from exc

    # Parse JSON from the text (robust to fences, surrounding prose, etc.)
    try:
        obj = _extract_json(text, require_key)
    except ValueError as exc:
        raise GeminiError(str(exc)) from exc

    return obj


def _extract_json(text: str, require_key: str) -> Dict[str, Any]:
    """Return the first complete JSON object carrying `require_key`.

    Handles ```json fences, surrounding prose, and smaller JSON-ish snippets
    (like an example object echoed from the prompt) that appear before the real
    answer. Uses json.JSONDecoder.raw_decode to read exactly one complete object
    and stop, then checks for the required key before accepting it.
    """
    text = text.strip()
    # Strip markdown code fences if present.
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            text = parts[1].strip()
        if text.startswith("json"):
            text = text[4:].lstrip()

    decoder = json.JSONDecoder()
    i = 0
    while True:
        brace = text.find("{", i)
        if brace == -1:
            raise ValueError(
                f"No JSON object with key {require_key!r} found in Gemini output: "
                f"{text[:300]!r}")
        try:
            obj, end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            i = brace + 1
            continue
        if isinstance(obj, dict) and require_key in obj:
            return obj
        i = end  # valid JSON but wrong shape — skip past it


# -- Weekly report narration ---------------------------------------------------

_COACH_RULES = """És um coach de nutrição atencioso e prático a falar com o utilizador
em português de Portugal (tratamento por "tu"). O objetivo dele é recomposição
corporal: perder gordura mantendo músculo, com proteína alta.

REGRAS ABSOLUTAS:
- Os FACTOS abaixo já foram calculados. NUNCA inventes nem recalcules um número; usa
  só os que te são dados. Se não há dado, não afirmes nada sobre isso.
- Escolhe UM único foco para a semana — o mais importante (já vem priorizado em
  `ranked_issues`). Nada de listas de 10 dicas; uma pessoa com 10 dicas faz zero.
- Celebra o que está a correr bem (`wins`) — o reforço é o que mantém um hábito.
- NÃO alarmes sobre o que a política diz ser benigno (ex.: colesterol alimentar, um
  excesso marcado como não-problema). Mede as palavras; sê calmo, não catastrófico.
- Sem linguagem médica nem diagnósticos. Se algo pede análises, sugere "vale a pena
  um exame", nunca um veredito.
- Enquadra a suficiência como vitória; nunca incentives comer menos por comer menos.
- Uma frase por campo. Concreto, caloroso, humano. Nada de jargão."""

_REPORT_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "headline": "uma frase que resume a semana",
  "wins": [{"title": "curto", "detail": "uma frase"}],
  "focus": {
    "key": "<a chave de ranked_issues[0]>",
    "label": "<nome do nutriente em pt-PT>",
    "why": "porque importa, numa frase, com o número relevante",
    "attribution": "de onde vem, se houver `attribution` (ex.: 68% vem do chouriço)",
    "severity": "high|medium|low"
  },
  "swap": {"from": "alimento a reduzir/atual", "to": "alternativa melhor e realista",
           "why": "uma frase — porquê"},
  "continuity": "uma frase sobre o progresso desde a última semana, OU null se não houver",
  "encouragement": "uma frase final, motivadora e humana"
}"""


def build_weekly_prompt(diagnosis: Dict[str, Any],
                        profile: List[Dict[str, Any]],
                        continuity: Optional[Dict[str, Any]] = None) -> str:
    """Assemble the prompt for the weekly coach."""
    top_foods = [f["food"] for f in profile[:20]]
    facts = {
        "window": diagnosis.get("window"),
        "adherence": diagnosis.get("adherence"),
        "ranked_issues": diagnosis.get("ranked_issues"),
        "nutrients": [
            n for n in diagnosis.get("nutrients", [])
            if n.get("genuine_issue") or n.get("status") in
            ("over_benign", "approaching_ul") or n.get("key") in
            diagnosis.get("ranked_issues", [])
        ],
        "wins": diagnosis.get("wins"),
        "coverage_note": diagnosis.get("coverage_note"),
        "basis": diagnosis.get("basis"),
        "continuity": continuity,
        "foods_the_user_eats": top_foods,
    }
    return (f"{_COACH_RULES}\n\nFACTOS (JSON, já calculados):\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n{_REPORT_SCHEMA}")


def narrate_weekly(diagnosis: Dict[str, Any], profile: List[Dict[str, Any]],
                   continuity: Optional[Dict[str, Any]] = None,
                   api_key: Optional[str] = None,
                   model: Optional[str] = None) -> Dict[str, Any]:
    """Generate the weekly coaching report via Gemini.

    Returns the report dict (headline, wins, focus, swap, etc.).
    Raises GeminiError on transport/parse failure.
    """
    prompt = build_weekly_prompt(diagnosis, profile, continuity)
    log.info("weekly prompt %d chars", len(prompt))

    draft = call_gemini(prompt, require_key="headline", api_key=api_key,
                        model=model, temperature=0.3)
    draft = _strip_meta(draft)

    # Critic pass — reconcile the prose against the diagnosis facts.
    try:
        report = _critic_pass(diagnosis, draft, api_key=api_key)
    except GeminiError as exc:
        log.warning("critic pass failed (non-fatal, keeping draft): %s", exc)
        report = draft

    return report


# -- Critic pass ---------------------------------------------------------------

_CRITIC_RULES = """És um revisor rigoroso. Recebes os FACTOS calculados e um RASCUNHO de
conselho. Verifica, sem simpatia:
1. Cada afirmação numérica do rascunho é suportada pelos factos? (nada inventado)
2. O nível de alarme condiz? Nada tratado como grave se a política o marca benigno.
3. Sem linguagem médica/diagnóstico. Sem incentivo a restringir por restringir.
4. Escolheu UM foco coerente com ranked_issues[0]?
Devolve APENAS: {"ok": true|false, "issues": ["..."], "report": {<o rascunho corrigido, mesma forma; se ok, devolve-o tal como está>}}"""


def build_critic_prompt(diagnosis: Dict[str, Any], report: Dict[str, Any]) -> str:
    facts = {
        "ranked_issues": diagnosis.get("ranked_issues"),
        "nutrients": diagnosis.get("nutrients"),
        "adherence": diagnosis.get("adherence"),
        "wins": diagnosis.get("wins"),
    }
    return (f"{_CRITIC_RULES}\n\nFACTOS:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"RASCUNHO:\n{json.dumps(report, ensure_ascii=False)}")


def _critic_pass(diagnosis: Dict[str, Any], draft: Dict[str, Any],
                 api_key: Optional[str] = None) -> Dict[str, Any]:
    """Run a critic pass on the draft, returning the (possibly corrected) report."""
    prompt = build_critic_prompt(diagnosis, draft)
    verdict = call_gemini(prompt, require_key="ok", api_key=api_key,
                          model=GEMINI_CRITIC_MODEL, temperature=0.1)
    if verdict.get("ok"):
        return draft
    corrected = verdict.get("report") or draft
    log.info("critic corrected draft: %s", "; ".join(verdict.get("issues", []))[:300])
    return _strip_meta(corrected)


# -- Next-meal narration (dynamic slot) ----------------------------------------

_NEXT_MEAL_V2_RULES = """És um coach de nutrição prático a responder à pergunta diária "o que
vou comer a seguir?" em português de Portugal ("tu"). Tens acesso ao dia atual da pessoa,
aos hábitos alimentares dela (horários típicos de refeição), e ao orçamento nutricional
que ainda falta.

A tua tarefa tem DUAS PARTES:

PARTE 1 — Decide qual é a PRÓXIMA REFEIÇÃO.
Analisa:
- A hora atual
- O que já foi registado hoje (se alguma coisa)
- Os horários típicos da pessoa (com que frequência e a que horas costuma comer cada
  tipo de refeição)
- O orçamento que resta (calorias, proteína)
- Os nutrientes que ainda estão em falta hoje

Com base nisto, decide qual deve ser a próxima refeição. Por exemplo:
- Se são 10:30 e ainda não comeu nada → pequeno-almoço (mesmo sendo tarde)
- Se já tomou pequeno-almoço e são 10:30 mas raramente come a meio da manhã → almoço
- Se já tomou pequeno-almoço e são 10:30 e costuma lanchar a meio da manhã →
  lanche da manhã (ligeiro)
- Se já almoçou e são 15:00 e ainda tem poucas calorias → lanche da tarde
- Se já almoçou e são 18:00 → jantar
- Se comeu um snack pesado ao lanche → o jantar deve ser mais leve para caber no
  orçamento do dia

PARTE 2 — Cria 3 sugestões de prato para essa refeição.
- Usa sobretudo comida que a pessoa já come (a lista `candidates`). Podes introduzir no
  MÁXIMO 1 alimento novo saudável e comum por sugestão — marca o novo com "new": true.
- Respeita as gramas dadas (grams_low..grams_high) para os alimentos candidatos. Não
  excedas as calorias que sobram.
- Cada prato diz, numa frase, o que resolve (o foco da semana e/ou a falha de hoje).
- Sê concreto: dá quantidades precisas (gramas) para cada ingrediente.
- O 1.º prato é o recomendado.
- Sê apetecível; isto tem de dar vontade de cozinhar.

Nota importante sobre ajuste após refeições inesperadas:
- Se a pessoa já comeu uma refeição pesada (muitas calorias, muita proteína, muita
  gordura), as sugestões seguintes devem ser mais leves para caber no orçamento.
- Se comeu algo muito ligeiro, mantém as sugestões normais.
- Usa o teu bom senso para avaliar o impacto do que já foi comido hoje."""

_PLATES_V2_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "next_slot": "pequeno-almoço | almoço | jantar | lanche da manhã | lanche da tarde",
  "reasoning": "explicação muito curta de porque este é o próximo slot",
  "plates": [
    {
      "rank": 1, "recommended": true,
      "title": "nome do prato",
      "items": [{"food": "...", "grams_low": N, "grams_high": N, "new": false}],
      "covers": [{"key": "omega3_g", "label": "Ómega-3", "note": "nota opcional curta"}],
      "calories": N, "protein_g": N,
      "why": "uma frase — o que resolve e porque encaixa"
    },
    {"rank": 2, "recommended": false, ...},
    {"rank": 3, "recommended": false, ...}
  ]
}"""


def build_next_meal_v2_prompt(context: Dict[str, Any],
                              profile: List[Dict[str, Any]]) -> str:
    """Assemble the prompt for the dynamic-slot next-meal generator.

    `context` must include:
      - current_time (HH:MM)
      - today_meals: list of meals logged today
      - meal_pattern: user's typical timing profile
      - calories_left, protein_left_g
      - shortfalls_today, focus_key
      - candidates: per-shortfall candidate foods with portion ranges
    """
    payload = {
        "current_time": context.get("current_time"),
        "today_meals": context.get("today_meals", []),
        "meal_pattern": context.get("meal_pattern", {}),
        "calories_left": context.get("calories_left"),
        "protein_left_g": context.get("protein_left_g"),
        "shortfalls_today": context.get("shortfalls_today", []),
        "focus_key": context.get("focus_key"),
        "candidates": context.get("candidates", {}),
        "top_foods": [
            {"food": f["food"], "category": f["category"],
             "times_eaten": f["times_eaten"]}
            for f in profile[:25]
        ],
    }
    return (f"{_NEXT_MEAL_V2_RULES}\n\nDADOS (JSON):\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n{_PLATES_V2_SCHEMA}")


def assemble_next_meal(context: Dict[str, Any],
                       profile: List[Dict[str, Any]],
                       api_key: Optional[str] = None,
                       model: Optional[str] = None) -> Dict[str, Any]:
    """Generate next-meal suggestions via Gemini, with dynamic slot detection.

    Returns dict with keys: next_slot, reasoning, plates.
    Raises GeminiError on transport/parse failure.
    """
    prompt = build_next_meal_v2_prompt(context, profile)
    log.info("next-meal prompt %d chars", len(prompt))

    result = call_gemini(prompt, require_key="plates", api_key=api_key,
                         model=model, temperature=0.3)
    return _strip_meta(result)


# -- Helpers -------------------------------------------------------------------

def _strip_meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in obj.items() if not k.startswith("_")}
