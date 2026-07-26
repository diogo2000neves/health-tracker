"""Gemini-powered narration for the Coach feature.

Replaces the local `claude` CLI with direct Gemini API calls. The deterministic
analysis already happened in `insights.py` — this module only turns finished facts
into human coaching prose. Can run in the backend (Cloud Run) or locally.

Generation modes, all called as top-level functions:

  narrate_cards(facts, ...)
      -> { cards: [{ kind, ref, title, body, chips, swap }] }

    The feed's prose. `facts` leads with the FOOD-LEVEL reading of the log (groups
    against their weekly references, streaks, per-slot composition, variety) and
    carries nutrient numbers only as supporting evidence. That ordering is the whole
    content fix: a prompt that opens with a nutrient table can only produce "eat
    more fibre", which is what the app's Nutrients tab already says.

  narrate_next_meal(context, ...)
      -> { next_slot, reasoning, plates: [{ title, items, covers, ... }] }

  chat_turn(context, history, message, ...)
      -> { reply, memory_candidates } — a conversation anchored to one card, which
    also notices anything durable worth remembering about the user.

  narrate_weekly(diagnosis, profile, continuity, ...)
      -> { headline, wins, focus, swap, continuity, encouragement }
    The original Sunday review, kept for the legacy /insights endpoints.

  critic_pass(diagnosis, draft)
      -> { ok, issues, report } — validates a weekly report against the facts.

Design invariants carried from Phase 2:
  * The model NEVER invents a number. Every numerical claim in the output must be
    traceable to the facts in the prompt.
  * The model NEVER invents a food. A swap's `from` must be something the user
    actually logged and its `to` must come from the offered options — enforced in
    code by `coach_feed._validated_swap`, because a prompt rule alone did not hold:
    the critic pass caught the model proposing "swap your white bread" for a user
    who had never logged bread.
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
# Default to the same model family the existing audit pipeline uses — the project's
# Gemini API plan has quota for `gemini-3.6-flash` but **not** for `gemini-2.0-flash`
# (that family is on a separate free-tier quota that was exhausted). Overridable.
GEMINI_MODEL = os.environ.get("GEMINI_NARRATOR_MODEL", "gemini-3.6-flash")
GEMINI_CRITIC_MODEL = os.environ.get("GEMINI_CRITIC_MODEL", "gemini-3.6-flash")
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


# -- Feed cards (food-first) ---------------------------------------------------

_CARD_RULES = """És o nutricionista pessoal desta pessoa, a escrever os cartões que ela vê
quando abre a app. Português de Portugal, tratamento por "tu". O objetivo dela é
recomposição corporal: perder gordura mantendo músculo, com proteína alta.

A REGRA CENTRAL — FALA DE COMIDA, NÃO DE NUTRIENTES.
O que interessa é o que a pessoa realmente comeu: alimentos, refeições, repetições, o que
falta na mesa. Um número de nutriente só pode entrar se estiver amarrado a um alimento
concreto ("o arroz branco quase todos os dias é o que está a travar a fibra"). NUNCA
escrevas conselhos como "come mais fibra" ou "aumenta a vitamina A" — isso a pessoa já vê
no ecrã dos nutrientes, e não a ajuda a decidir o que põe no prato.

REGRAS ABSOLUTAS:
- Só podes falar de alimentos que aparecem em `foods_the_user_eats` ou em
  `findings[].swap_options`. Não inventes alimentos nem marcas.
- Numa troca (`swap`): o `from` TEM de ser um alimento que a pessoa registou, e o `to` TEM
  de ser um dos alimentos em `swap_options.to` desse finding. Se não houver uma troca
  honesta a fazer, devolve `swap: null`.
- Nunca inventes nem recalcules números. Usa só os que estão nos FACTOS.
- Um cartão = uma ideia. Título curto; corpo de 1 a 2 frases.
- Nada de linguagem médica nem diagnósticos. Nada de "corta", "elimina", "nunca mais":
  enquadra sempre como acrescentar ou trocar, nunca como restringir por restringir.
- Não repitas nada que esteja em `already_said_recently`.
- Usa `memory` (o que já sabes desta pessoa) quando for relevante — é isso que faz o
  conselho parecer dirigido a ela e não a um manual.
- Caloroso, direto, sem jargão e sem emojis.

OS TIPOS DE CARTÃO que te podem ser pedidos (em `wanted_cards`):
- `day_plan` (manhã): o dia que começa, à luz do que aconteceu ontem. Uma ação concreta.
- `check_in` (tarde): o que já foi comido hoje e a oportunidade que ainda resta hoje.
- `day_summary` (noite): fecha o dia com honestidade e deixa uma nota para amanhã.
- `weekly_review` (domingo): a semana em alimentos — o padrão mais importante e uma troca.
- `win`: algo que está genuinamente a correr bem, dito em alimentos.
- `pattern`: UMA observação sobre um padrão alimentar. Tens de escolher um dos `findings`
  e pôr o `id` dele em `ref`; o teu texto tem de dizer o mesmo que o `fact` desse finding,
  só em linguagem humana. Um cartão `pattern` por finding, no máximo."""

_CARDS_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "cards": [
    {
      "kind": "day_plan|check_in|day_summary|weekly_review|win|pattern",
      "ref": "<o id do finding — só para kind=pattern; caso contrário \\"\\">",
      "title": "título curto (máx. 60 caracteres)",
      "body": "1 a 2 frases",
      "chips": [{"label": "facto muito curto (máx. 24 caracteres)",
                 "tone": "good|warn|bad|neutral"}],
      "swap": {"from": "alimento registado", "to": "alimento das swap_options",
               "why": "uma frase"}
    }
  ]
}
Um cartão para cada tipo pedido em `wanted_cards`, na ordem em que aparecem. `chips` e
`swap` são opcionais (`swap: null` quando não houver troca honesta)."""


def build_cards_prompt(facts: Dict[str, Any]) -> str:
    return (f"{_CARD_RULES}\n\nFACTOS (JSON, já calculados):\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}\n\n"
            f"{_CARDS_SCHEMA}")


def narrate_cards(facts: Dict[str, Any], api_key: Optional[str] = None,
                  model: Optional[str] = None) -> Dict[str, Any]:
    """The prose for one generation slot, in a single call.

    One call per slot (not one per card) keeps the whole feed coherent — the cards
    read like one person wrote them in one sitting, and they can avoid repeating each
    other because the model sees them all at once.
    """
    prompt = build_cards_prompt(facts)
    log.info("cards prompt %d chars (slot=%s)", len(prompt), facts.get("slot"))
    return _strip_meta(call_gemini(prompt, require_key="cards", api_key=api_key,
                                   model=model, temperature=0.35))


# -- Next meal (food-aware, always answerable) ---------------------------------

_NEXT_MEAL_RULES = """És um coach de nutrição prático a responder à pergunta diária "o que
vou comer a seguir?" em português de Portugal ("tu").

PARTE 1 — Decide qual é a PRÓXIMA REFEIÇÃO, a partir da hora atual, do que já foi
registado hoje, dos horários típicos da pessoa (`meal_pattern`) e do orçamento que resta.
Exemplos: são 10:30 e ainda não comeu nada -> pequeno-almoço, mesmo tarde; já almoçou e
são 15:00 -> lanche da tarde; já almoçou e são 18:30 -> jantar.

PARTE 2 — Cria 3 sugestões de prato para essa refeição.
- Usa sobretudo comida que a pessoa já come: `candidates.usual_at_this_slot`,
  `candidates.by_nutrient` e `candidates.for_findings`.
- `candidates.groups_to_favour` são os grupos alimentares que andam abaixo da referência
  semanal — puxa por eles quando encaixar na refeição (é aqui que o peixe entra num dia
  em que só houve carne).
- Podes introduzir no MÁXIMO 1 alimento novo, saudável e comum por sugestão — marca-o com
  "new": true. Os alimentos em `candidates` marcados `new` também contam como novos.
- Respeita as gramas dadas (grams_low..grams_high) quando existirem, e não excedas as
  calorias que restam.
- Respeita `memory`: se a pessoa disse que não gosta de algo ou que não pode comer algo,
  não o sugiras.
- Sê apetecível e concreto: isto tem de dar vontade de cozinhar.
- O 1.º prato é o recomendado.

MUITO IMPORTANTE: devolves SEMPRE 3 pratos. Mesmo que hoje não falte nenhum nutriente,
"o que como a seguir?" continua a ser uma pergunta legítima — nesse caso sugere pratos
equilibrados, com variedade, que puxem pelos grupos em falta na semana e respeitem o
orçamento que resta. Nunca respondas que não é preciso sugestão."""

_PLATES_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "next_slot": "pequeno-almoço | almoço | jantar | lanche da manhã | lanche da tarde",
  "reasoning": "uma frase curta — porque é esta a próxima refeição",
  "plates": [
    {
      "rank": 1, "recommended": true,
      "title": "nome do prato",
      "items": [{"food": "...", "grams_low": N, "grams_high": N, "new": false}],
      "covers": [{"key": "omega3_g", "label": "Ómega-3", "note": "nota curta"}],
      "calories": N, "protein_g": N,
      "why": "uma frase — o que resolve e porque encaixa"
    },
    {"rank": 2, "recommended": false, "...": "..."},
    {"rank": 3, "recommended": false, "...": "..."}
  ]
}"""


def build_next_meal_prompt(context: Dict[str, Any]) -> str:
    """`context` carries current_time, today_meals, meal_pattern, the remaining
    budget, the food-level candidates (see coach_feed.next_meal_candidates) and the
    user's memory."""
    return (f"{_NEXT_MEAL_RULES}\n\nDADOS (JSON):\n"
            f"{json.dumps(context, ensure_ascii=False, indent=1, default=str)}\n\n"
            f"{_PLATES_SCHEMA}")


def narrate_next_meal(context: Dict[str, Any], api_key: Optional[str] = None,
                      model: Optional[str] = None) -> Dict[str, Any]:
    prompt = build_next_meal_prompt(context)
    log.info("next-meal prompt %d chars", len(prompt))
    return _strip_meta(call_gemini(prompt, require_key="plates", api_key=api_key,
                                   model=model, temperature=0.35))


# -- Chat (anchored to a card, with memory) ------------------------------------

_CHAT_RULES = """És o nutricionista pessoal desta pessoa, a conversar com ela sobre um
conselho que lhe deste. Português de Portugal, tratamento por "tu".

- Responde curto: 2 a 4 frases. É uma conversa, não um artigo.
- Fala de comida concreta — alimentos, porções, refeições — e usa os FACTOS que te são
  dados. Não inventes números nem alimentos que a pessoa não registou.
- Se a pergunta não for suportada pelos factos que tens, di-lo com honestidade em vez de
  adivinhar ("não tenho isso registado, mas...").
- Nada de diagnósticos nem de linguagem médica. Se a pergunta for clínica (análises,
  medicação, sintomas), diz que vale a pena falar com um médico ou nutricionista e
  responde só à parte alimentar.
- Nunca incentives comer menos por comer menos. Recomposição faz-se com proteína alta e
  comida suficiente.
- Usa `memory` para não voltar a perguntar o que já sabes.

MEMÓRIA: se nesta mensagem a pessoa revelar algo DURADOURO sobre ela — uma preferência,
uma coisa que não gosta, uma restrição, um objetivo, uma rotina, uma limitação de tempo ou
de cozinha — devolve-o em `memory_candidates`. Só o que continuará verdade daqui a um mês:
"não gosta de peixe cozido" sim; "hoje não lhe apetece peixe" não."""

_CHAT_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "reply": "a tua resposta, 2 a 4 frases",
  "memory_candidates": [
    {"type": "preference|dislike|constraint|goal|routine",
     "fact": "uma frase curta, na 3.ª pessoa (ex.: \\"não gosta de peixe cozido\\")",
     "confidence": 0.0}
  ]
}"""


def build_chat_prompt(context: Dict[str, Any], history: List[Dict[str, Any]],
                      message: str) -> str:
    turns = "\n".join(
        f"{'PESSOA' if t.get('role') == 'user' else 'TU'}: {t.get('text', '')}"
        for t in history[-12:])
    return (f"{_CHAT_RULES}\n\nFACTOS (JSON):\n"
            f"{json.dumps(context, ensure_ascii=False, indent=1, default=str)}\n\n"
            f"CONVERSA ATÉ AGORA:\n{turns or '(nenhuma)'}\n\n"
            f"NOVA MENSAGEM DA PESSOA:\n{message}\n\n{_CHAT_SCHEMA}")


def chat_turn(context: Dict[str, Any], history: List[Dict[str, Any]],
              message: str, api_key: Optional[str] = None,
              model: Optional[str] = None) -> Dict[str, Any]:
    """One reply, plus anything durable the message revealed about the user.

    Extraction rides along on the same call rather than a second pass: it is the
    same reasoning ("what did they just tell me about themselves?"), and one call
    keeps a chat turn inside the couple of seconds a conversation can tolerate.
    """
    prompt = build_chat_prompt(context, history, message)
    log.info("chat prompt %d chars", len(prompt))
    answer = _strip_meta(call_gemini(prompt, require_key="reply", api_key=api_key,
                                      model=model, temperature=0.4))
    candidates = answer.get("memory_candidates")
    return {"reply": str(answer.get("reply") or "").strip(),
            "memory_candidates": candidates if isinstance(candidates, list) else []}


# -- Helpers -------------------------------------------------------------------

def _strip_meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in obj.items() if not k.startswith("_")}
