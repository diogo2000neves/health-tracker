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
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("narrator")

# -- config --------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Free-tier quota is per model family and is shared with the meal-analysis pipeline,
# so the right default is simply "whichever family still has allowance". As of
# 2026-07-26 `gemini-3.6-flash`, `gemini-2.0-flash` and `gemini-flash-latest` all
# answer 429 for this key while `gemini-3-flash-preview` has room. Overridable per
# environment, which is the intended way to move it when this changes again.
GEMINI_MODEL = os.environ.get("GEMINI_NARRATOR_MODEL", "gemini-3-flash-preview")
GEMINI_CRITIC_MODEL = os.environ.get("GEMINI_CRITIC_MODEL", "gemini-3-flash-preview")
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "90"))
# How many times to wait out a 429 before giving up. Two is enough for the free
# tier's per-minute window without letting one generation sit on a worker for
# minutes — anything worse than that is better handled by the queue retrying later.
QUOTA_RETRIES = int(os.environ.get("GEMINI_QUOTA_RETRIES", "2"))
# Cap on how long a single 429 wait may be, whatever the API asks for.
QUOTA_MAX_WAIT_S = 45.0


# -- Gemini API transport ------------------------------------------------------

class GeminiError(RuntimeError):
    """Any transport/parse failure. Callers catch this and degrade gracefully."""


class GeminiQuotaError(GeminiError):
    """The API said 429 — we are inside the free tier's per-minute allowance.

    Worth its own type because it means "ask again shortly", not "this failed".
    The caller turns it into a retry rather than an empty feed: the coach shares its
    quota with the meal-analysis pipeline, so a burst of logged meals can easily push
    a scheduled generation over the limit for a few seconds.
    """


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

    # The free tier allows a small number of requests per minute, and the coach shares
    # that allowance with the meal-analysis pipeline — so a 429 is a routine "wait a
    # moment", not a failure. The API tells us how long to wait; honour it once or
    # twice rather than dropping the generation on the floor.
    raw = None
    for attempt in range(QUOTA_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code == 429:
                if attempt >= QUOTA_RETRIES:
                    raise GeminiQuotaError(f"Gemini quota exhausted: {detail}") from exc
                delay = _retry_delay_s(detail, attempt)
                log.info("Gemini quota hit; retrying in %.1fs", delay)
                time.sleep(delay)
                continue
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


def _retry_delay_s(detail: str, attempt: int) -> float:
    """How long to wait after a 429.

    The API's own message carries the answer ("Please retry in 26.9s"), which beats
    guessing — a blind exponential backoff either wakes up too early and burns another
    request, or sleeps far longer than the window actually needs.
    """
    match = re.search(r"retry in ([0-9.]+)s", detail)
    if match:
        try:
            return min(float(match.group(1)) + 1.0, QUOTA_MAX_WAIT_S)
        except ValueError:
            pass
    return min(5.0 * (2 ** attempt), QUOTA_MAX_WAIT_S)


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
- Os nomes dos alimentos nos FACTOS já vêm em português de Portugal. Usa-os TAL E
  QUAL; nunca escrevas o nome de um alimento em inglês.
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
    # Already pt-PT: `insights.build_food_profile` keys the vocabulary on the display
    # name, so the report can quote these straight into Portuguese prose.
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
- Usa o teu bom senso para avaliar o impacto do que já foi comido hoje.

Nomes de alimentos: os DADOS já trazem tudo em português de Portugal. Escreve-os TAL
E QUAL, incluindo em `items[].food` e nos títulos dos pratos. Nunca em inglês."""

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


# -- The feed, in one call ------------------------------------------------------
#
# Plates and cards are generated TOGETHER, deliberately. Split across two calls they
# contradicted each other in production on the first day: the next-meal card proposed
# fish while the day card, generated blind to it, told the user to finish the day with
# beef. One call sees the whole feed at once, so the day analysis can reference the
# suggestion it just made.

_FEED_RULES = """És o treinador pessoal desta pessoa — nutrição em primeiro lugar, e
também o resto do que ela mede — a escrever tudo o que ela vê quando abre a app.
Português de Portugal, tratamento por "tu".

UM CARTÃO, UMA IDEIA — mas podes escrever vários cartões.
Cada cartão diz UMA coisa e propõe UMA ação: não metas sono, treino e comida no mesmo
cartão. Mas se hoje há mesmo coisas importantes em áreas diferentes, escreve um cartão
para cada uma — os `findings` que recebes já foram filtrados por importância, por isso
tudo o que aí está merece ser dito. O que não podes é encher: três observações mornas
valem menos do que uma boa, e se não tiveres nada de substancial para um tipo de
cartão, não o escrevas — o silêncio é uma resposta legítima e a app sabe mostrá-lo.

A REGRA CENTRAL — FALA DE COMIDA, NÃO DE NUTRIENTES.
O que interessa é o que a pessoa realmente comeu: alimentos concretos, refeições,
repetições, o que falta na mesa. Um número de nutriente só entra se estiver amarrado a um
alimento ("o arroz branco quase todos os dias é o que está a travar a fibra"). NUNCA
escrevas "come mais fibra" ou "aumenta a vitamina A" — isso ela já vê no ecrã dos
nutrientes e não a ajuda a decidir o que põe no prato.

PROFUNDIDADE — é isto que distingue um bom conselho de um relatório.
Tens em `today.meals` o detalhe completo de cada refeição de hoje: alimentos, gramas,
calorias, macros e micronutrientes. Usa-o para avaliar ESCOLHAS, não para repetir totais:
- diz porque é que uma escolha concreta foi boa ("a aveia com manteiga de amendoim ao
  pequeno-almoço segurou-te a manhã inteira"),
- avalia o equilíbrio de uma refeição ("o almoço trouxe proteína e hidratos, mas foi o
  terceiro dia seguido sem nada verde"),
- e só depois diz o passo seguinte.
"Estás bem na proteína, mete mais proteína ao jantar" é exatamente o tipo de comentário
genérico a evitar.

ACONTECIMENTOS — lê `today_events` e as notas em `today.meals[].your_note`.
A pessoa escreve nas notas o contexto que a lista de alimentos não tem ("Comi um menu
médio Big Tasty do McDonalds", "Exagerei hoje"). Usa-o:
- Uma noite com bebidas é um convívio, não um falhanço nutricional. Reconhece o que
  foi, sem drama e sem sermão, e dá o passo prático a seguir (água, a refeição
  seguinte, o treino de amanhã) — como faria um nutricionista com quem se tem
  confiança.
- Fast food acontece. Diz porquê é que pesa (o quê, em números, nessa refeição) e
  como se gere — não que "não devia".
- Se a pessoa já disse que sabe que exagerou, NÃO repitas a lição. Ela já a sabe;
  responde ao que ela disse.

MEMÓRIA — `memory` traz o que já sabes desta pessoa: `about_you` (preferências,
restrições), `said_recently` (o que já lhe disseste — não repitas), `you_might_recall`
(episódios passados relevantes para hoje) e `previous_reports` (as revisões semanais e
mensais). Se hoje se repete algo que já aconteceu, di-lo com a memória: "é a terceira
sexta-feira seguida assim" vale mais do que qualquer conselho genérico.

COERÊNCIA — tudo o que escreves é lido ao mesmo tempo, no mesmo ecrã.
As sugestões de refeição e os cartões TÊM de dizer a mesma coisa. Se sugerires peixe ao
jantar, o cartão do dia não pode mandar comer carne. Não te repitas entre cartões, e não
repitas nada que esteja em `already_said_recently`.

REGRAS ABSOLUTAS:
- Só podes falar de alimentos que aparecem em `foods_the_user_eats`, em
  `findings[].swap_options` ou em `next_meal.candidates`. Não inventes alimentos.
- Os nomes dos alimentos nos FACTOS já vêm em português de Portugal. Usa-os TAL E
  QUAL, incluindo em `swap`, nos títulos dos pratos e em `items[].food`. Nunca
  escrevas o nome de um alimento em inglês, nem traduzas um nome que já te é dado.
- Numa troca (`swap`): o `from` TEM de ser um alimento que a pessoa registou e o `to` TEM
  de vir das `swap_options.to` desse finding. Se não houver troca honesta, `swap: null`.
- A troca tem de fazer sentido À MESA: cada alimento traz a refeição em que é
  realmente comido (`usually_at`). Não substituas o fiambre da sanduíche do
  pequeno-almoço por bacalhau — troca-o por algo que se coma ao pequeno-almoço (ovo,
  queijo fresco, atum, manteiga de amendoim). Uma troca que ninguém faria na vida real
  é pior do que não sugerir troca nenhuma.
- Nunca inventes nem recalcules números. Usa só os que estão nos FACTOS.
- Nada de linguagem médica nem diagnósticos. Nada de "corta" ou "elimina": enquadra
  como acrescentar ou trocar, nunca como restringir por restringir.
- Usa `memory` (o que já sabes desta pessoa) quando for relevante.
- Caloroso, direto, sem jargão e sem emojis.

PARTE 1 — A PRÓXIMA REFEIÇÃO (`next_meal`).
Decide qual é, a partir da hora atual, do que já foi registado hoje, dos horários
típicos (`next_meal.meal_pattern`) e do orçamento que resta. Depois cria 3 pratos:
- sobretudo comida que a pessoa já come (`next_meal.candidates`);
- `candidates.groups_to_favour` são os grupos abaixo da referência semanal — puxa por
  eles quando encaixar na refeição (é aqui que o peixe entra num dia só de carne);
- no MÁXIMO 1 alimento novo por prato, marcado `"new": true`;
- respeita as gramas dadas e as calorias que restam;
- respeita `memory` (o que a pessoa não gosta ou não pode comer);
- o 1.º prato é o recomendado.
Escreve também `rationale`: UMA frase curta que explique porque é que estas sugestões
encaixam na situação dela AGORA (o que já comeu hoje, o que falta, o que a semana pede).
Devolves SEMPRE 3 pratos — "o que como a seguir?" é sempre uma pergunta legítima, mesmo
num dia em que não falte nenhum nutriente.

PARTE 2 — OS CARTÕES (`cards`), um por cada tipo pedido em `wanted_cards`:
- `day_plan` (manhã): o dia que começa à luz de ontem. Uma ação concreta.
- `check_in` (a meio do dia): avalia as refeições de hoje uma a uma — o que foi boa
  escolha e porquê, o que ficou desequilibrado — e só então o passo seguinte.
- `day_summary` (fim do dia): o dia inteiro fechado — e este é o cartão onde o dia se
  vê TODO junto. Aqui SIM podes usar os totais (refeições, calorias, macros), mas
  amarrados às escolhas que os produziram. Se houver dados de treino, sono ou corpo em
  `body_and_activity`, fecha o dia com eles também: o que correu bem e o que correu
  mal, em cada área que a pessoa mede. Acaba com uma nota para amanhã.
- `weekly_review` (domingo): a semana inteira — o padrão mais importante na
  alimentação e, se existirem, o que aconteceu no treino, no sono e no corpo. É o
  momento de ligar as áreas umas às outras, não de as listar.
- `win`: algo que está genuinamente a correr bem. Pode ser comida, mas também pode ser
  treino, sono ou composição corporal — o que estiver mesmo a correr bem hoje. Uma
  pessoa que só ouve o que está mal deixa de abrir a app.
- `pattern`: UMA observação sobre um padrão. Escolhe um dos `findings` e põe o `id` dele
  em `ref`; o texto tem de dizer o mesmo que o `fact` desse finding, em linguagem humana.
- `link`: UMA ligação medida entre duas áreas da vida da pessoa. Só existe quando há um
  finding com `domain: "link"`; põe o `id` dele em `ref`."""


# -- the specialist frames -----------------------------------------------------
#
# The problem this solves: one prompt carrying every domain writes the AVERAGE of its
# payload. Leading with a nutrient table produced only nutrient advice (which is why
# food patterns lead the facts today), and simply adding sleep and training numbers
# to that same prompt would dilute all of them equally — a coach that says three
# shallow things instead of one deep one.
#
# So the prompt is ASSEMBLED, not fixed: only the frames for the domains that
# actually produced a finding this run are included. A sleep card is written by a
# prompt carrying sleep science and sleep-specific rules; a day with nothing but food
# findings gets exactly the prompt the coach had before this existed. One model call
# either way — specialisation is which frame is selected, not how many models run.
#
# Each frame says what depth looks like in that domain and, just as importantly,
# what the cheap generic version sounds like so it can be avoided by name.

_FRAME_NUTRITION = """ALIMENTAÇÃO — o que torna um conselho bom aqui.
Fala de comida concreta e de escolhas, nunca de nutrientes em abstrato. O detalhe está
em `today.meals`: alimentos, gramas, macros e micronutrientes de cada refeição. Avalia
escolhas ("a aveia com manteiga de amendoim segurou-te a manhã"), o equilíbrio de cada
refeição, e só depois o passo seguinte.
GENÉRICO A EVITAR: "come mais fibra", "aumenta a proteína ao jantar"."""

_FRAME_SLEEP = """SONO E RECUPERAÇÃO — o que torna um conselho bom aqui.
Lê sempre contra a linha de base DESTA pessoa (`evidence.personal_average` e
`evidence.line`), nunca contra um número de revista: 7h é muito para uns e pouco para
outros, e 73 ms de HRV não querem dizer nada sem o histórico dela.
O que conta mais, por esta ordem: a REGULARIDADE da hora de deitar (mexe mais no sono
profundo do que o total), a eficiência (tempo na cama que não é sono), e só depois a
duração. Uma noite má é terça-feira; o que interessa é o padrão que `evidence` mostra.
Liga sempre ao que a pessoa controla — a hora do jantar, o treino, a cafeína, a hora a
que se deita. É aí que a alimentação e o sono se tocam, e é esse o conselho útil.
GENÉRICO A EVITAR: "tenta dormir 8 horas", "melhora a higiene do sono".
NUNCA diagnostiques. Frequência cardíaca em repouso, HRV, SpO2 e temperatura da pele
descrevem padrões de recuperação — não são sinais clínicos e não se fala deles como
tal. Se algo parecer persistente, o passo é falar com um médico, dito uma vez e sem
alarme."""

_FRAME_ACTIVITY = """ATIVIDADE E TREINO — o que torna um conselho bom aqui.
O que importa para o objetivo dela é o TREINO DE FORÇA: é o que decide se o peso que
sai é gordura ou músculo. Passos e minutos ativos são contexto, não o tema.
Liga o treino à comida do dia: um dia de treino é o dia em que a proteína e os hidratos
mais contam, e um dia de descanso não precisa das mesmas calorias.
GENÉRICO A EVITAR: "faz mais exercício", "tenta dar 10 000 passos"."""

_FRAME_BODY = """COMPOSIÇÃO CORPORAL — o que torna um conselho bom aqui.
O peso sozinho não diz nada: 2 kg a menos é uma vitória ou um problema consoante o
tecido que saiu. Fala sempre do par massa magra / massa gorda, que é o que
`evidence` traz.
Uma leitura isolada é ruído (a bioimpedância varia com a hidratação) — só a tendência
de semanas conta, e a `evidence` já vem calculada assim. Não recalcules nada.
Quando está a correr bem, DIZ-LHE. É o objetivo dela a acontecer.
GENÉRICO A EVITAR: comentar a variação de um dia, ou falar de peso sem falar de músculo."""

_FRAME_DIGESTION = """DIGESTÃO — o que torna um conselho bom aqui.
É um tema banal e prático: trata-o com naturalidade, sem rodeios e sem drama. A alavanca
é quase sempre a fibra, a água e a regularidade das refeições — coisas que já estão nos
factos. Uma frase chega.
NUNCA faças medicina disto."""

_FRAME_LINK = """AS LIGAÇÕES (`domain: "link"`) — o cartão mais valioso que escreves.
É uma relação MEDIDA nos dados desta pessoa, entre duas áreas da vida dela, que ela
nunca conseguiria ver sozinha. `evidence` traz tudo: `cause`, `effect`, `effect_delta`
(o efeito nas unidades reais), `n_days`, `lag_days` e o `mechanism` — a explicação
fisiológica.
Como se escreve:
1. o facto, com o número real ("nas noites a seguir aos jantares mais tardios, menos
   18 minutos de sono profundo");
2. o porquê, a partir do `mechanism` — e SÓ a partir dele;
3. uma coisa concreta para fazer.
REGRAS DURAS:
- Escreve sempre como ASSOCIAÇÃO, nunca como causa: "nos dias em que…", "quando…",
  "isto anda a par de…". Nunca "isto causa", nunca "por causa disto".
- Não inventes ligações. Só podes falar de uma ligação que esteja nos `findings` com
  `domain: "link"`. Se não houver nenhuma, não escrevas nenhum cartão `link`.
- Não expliques o mecanismo por tuas palavras se ele contradisser o `mechanism` dado.
- Números vêm da `evidence` tal e qual. Nunca recalcules."""

_DOMAIN_FRAMES = {
    "nutrition": _FRAME_NUTRITION,
    "sleep": _FRAME_SLEEP,
    "activity": _FRAME_ACTIVITY,
    "body": _FRAME_BODY,
    "digestion": _FRAME_DIGESTION,
    "link": _FRAME_LINK,
}

# Order matters: food stays first because it is the daily spine of the app and the
# thing the user opens it for.
_FRAME_ORDER = ("nutrition", "link", "sleep", "activity", "body", "digestion")


def _frames_for(facts: Dict[str, Any]) -> str:
    """The specialist sections this generation needs — the domains that actually
    produced a finding, plus nutrition, which is always in play.

    This is the whole depth-without-dilution mechanism: on a day whose findings are
    all about food the prompt is exactly the one the coach had before, and on a day
    when sleep is the story the model is handed sleep expertise instead of a longer
    list of everything.
    """
    present = {"nutrition"}
    for finding in facts.get("findings") or ():
        if isinstance(finding, dict) and finding.get("domain"):
            present.add(str(finding["domain"]))
    return "\n\n".join(_DOMAIN_FRAMES[d] for d in _FRAME_ORDER if d in present)


def _blind_spots(facts: Dict[str, Any]) -> str:
    """Tell the model, in words, what this user does not measure.

    Silence is not enough. A model given no sleep data still writes confident sleep
    advice — it has read a million articles that do. Naming the gap is what makes a
    nutrition-only user's coach genuinely a nutrition coach rather than a general
    wellness one that happens to be short of numbers.
    """
    caps = facts.get("capabilities") or {}
    missing = list(caps.get("blind_spots") or ())
    if not missing:
        return ""
    labels = {"sleep": "sono e recuperação", "activity": "atividade e treino",
              "body": "composição corporal (peso, massa gorda, massa magra)",
              "digestion": "digestão", "nutrition": "alimentação"}
    named = ", ".join(labels.get(d, d) for d in missing)
    return (f"\n\nO QUE NÃO VÊS — esta pessoa não mede: {named}.\n"
            f"Não tens dados nenhuns sobre isso. Não faças perguntas sobre isso, não "
            f"assumas nada e não dês conselhos nessas áreas, nem de passagem. O teu "
            f"trabalho aqui é inteiramente sobre o que ela regista.")


def _goal_line(facts: Dict[str, Any]) -> str:
    """The user's actual goal, which is a different axis from what they measure."""
    caps = facts.get("capabilities") or {}
    label = str(caps.get("goal_label_pt") or "").strip()
    return f"\n\nO OBJETIVO DESTA PESSOA: {label}." if label else ""

_FEED_SCHEMA = """Devolve APENAS um objeto JSON com esta forma exata:
{
  "next_meal": {
    "next_slot": "pequeno-almoço | almoço | jantar | lanche da manhã | lanche da tarde",
    "reasoning": "uma frase — porque é esta a próxima refeição",
    "rationale": "uma frase — porque é que estas sugestões encaixam no teu dia de hoje",
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
  },
  "cards": [
    {
      "kind": "day_plan|check_in|day_summary|weekly_review|win|pattern|link",
      "ref": "<o id do finding — para kind=pattern e kind=link; caso contrário \"\">",
      "title": "título curto (máx. 60 caracteres)",
      "body": "2 a 4 frases com substância",
      "chips": [{"label": "facto muito curto (máx. 24 caracteres)",
                 "tone": "good|warn|bad|neutral"}],
      "swap": {"from": "alimento registado", "to": "alimento das swap_options",
               "why": "uma frase"}
    }
  ]
}
Omite `next_meal` quando `wanted_next_meal` for false. `chips` e `swap` são opcionais.

Responde com o JSON na tua resposta, e mais nada — sem preâmbulo, sem resumo, sem
escrever ficheiros."""


def build_feed_prompt(facts: Dict[str, Any]) -> str:
    """The one prompt behind the whole feed — built on the server so that Sonnet (on
    the Mac) and Gemini (the fallback) are given byte-identical instructions and their
    answers pass through byte-identical validation.

    Assembled per generation rather than constant: the shared voice and rules, then
    the goal, then the blind spots, then only the specialist frames for the domains
    that actually have something to say today. A day of pure food findings produces
    the same prompt this had before domains existed.
    """
    return (f"{_FEED_RULES}"
            f"{_goal_line(facts)}"
            f"{_blind_spots(facts)}\n\n"
            f"{_frames_for(facts)}\n\n"
            f"FACTOS (JSON, já calculados):\n"
            f"{json.dumps(facts, ensure_ascii=False, indent=1, default=str)}\n\n"
            f"{_FEED_SCHEMA}")


def narrate_feed(facts: Dict[str, Any], api_key: Optional[str] = None,
                 model: Optional[str] = None) -> Dict[str, Any]:
    """Generate the whole feed with Gemini. The fallback path — see
    `automation/coach/worker.py` for the Sonnet-on-the-Mac primary."""
    prompt = build_feed_prompt(facts)
    log.info("feed prompt %d chars (slot=%s)", len(prompt), facts.get("slot"))
    return _strip_meta(call_gemini(prompt, require_key="cards", api_key=api_key,
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
