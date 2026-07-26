"""What the coach remembers about the user between conversations.

The point of a coach that chats is that it stops asking the same questions. So every
chat turn can propose durable facts ("doesn't like boiled fish", "cooks in fifteen
minutes on weeknights", "training four times a week"), and those facts are folded in
here and injected into every later generation — the feed cards included, which is
what makes the daily advice get more personal rather than just more frequent.

Three deliberate constraints:

  * **Durable only.** A candidate has to be something still true in a month. The
    prompt draws the line ("doesn't like boiled fish" yes; "doesn't feel like fish
    today" no) and `merge` enforces a confidence floor on top.
  * **Deduplicated by meaning, not by string.** The same fact will arrive phrased
    differently; near-duplicates fold into one entry whose `mentions` grows, which
    doubles as the ranking signal for what to keep.
  * **Bounded and inspectable.** At most `MAX_FACTS` entries, and every one is
    readable and deletable from the app. A memory the user can't see is a memory
    they can't correct, and a wrong fact that silently shapes advice forever is
    worse than no memory at all.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = 1

# Enough to feel known, small enough to stay inside a prompt and to be readable in
# one screen in the app.
MAX_FACTS = 40

# Below this the model was guessing, and a guessed "fact" about someone is worse
# than silence.
MIN_CONFIDENCE = 0.5

TYPES = ("preference", "dislike", "constraint", "goal", "routine", "context")

# Negations are deliberately NOT stopwords: dropping "não" would make "gosta de
# peixe" and "não gosta de peixe" the same fact, and the coach would then confidently
# act on the opposite of what the user said.
_STOP = {"de", "do", "da", "dos", "das", "a", "o", "os", "as", "um", "uma", "e",
         "que", "com", "em", "para", "por", "se", "ao", "no", "na", "the", "of",
         "to", "is", "it"}


def empty() -> Dict[str, Any]:
    return {"version": VERSION, "facts": []}


def _norm(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFD", str(text or ""))
                   if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9\s]", " ", text)


def _tokens(text: str) -> set:
    return {w for w in _norm(text).split() if w and w not in _STOP and len(w) > 2}


def fact_id(fact: str) -> str:
    return "m-" + hashlib.sha256(_norm(fact).encode("utf-8")).hexdigest()[:12]


def _same_meaning(a: str, b: str) -> bool:
    """Whether two phrasings are the same fact.

    Jaccard over content words: cheap, no model call, and right for the cases that
    actually occur ("não gosta de peixe cozido" vs "não gosta de peixe cozido ao
    jantar"). Being slightly too eager to merge is the safer failure — it keeps the
    list short and honest instead of accumulating six versions of one preference.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    return overlap >= 0.6


def merge(memory: Optional[Dict[str, Any]], candidates: Sequence[Dict[str, Any]], *,
          today: str, source: str = "chat") -> Tuple[Dict[str, Any], int]:
    """Fold chat-proposed facts into the memory. Returns (memory, number added).

    A candidate that matches an existing fact bumps its `mentions` and `last_seen`
    rather than adding a row — repetition is evidence, not noise.
    """
    out = dict(memory or empty())
    out.setdefault("version", VERSION)
    facts: List[Dict[str, Any]] = [f for f in (out.get("facts") or [])
                                   if isinstance(f, dict) and f.get("fact")]
    added = 0

    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        text = " ".join(str(candidate.get("fact") or "").split())[:160]
        if not text:
            continue
        try:
            confidence = float(candidate.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        if confidence < MIN_CONFIDENCE:
            continue
        kind = str(candidate.get("type") or "context")
        if kind not in TYPES:
            kind = "context"

        # Same wording AND same kind: "gosta de peixe" (preference) and "não gosta de
        # peixe" (dislike) share most of their words and must never fold together.
        existing = next((f for f in facts
                         if f.get("type") == kind
                         and _same_meaning(f["fact"], text)), None)
        if existing:
            existing["mentions"] = int(existing.get("mentions") or 1) + 1
            existing["last_seen"] = today
            existing["confidence"] = round(
                max(float(existing.get("confidence") or 0), confidence), 2)
            continue

        facts.append({
            "id": fact_id(text), "type": kind, "fact": text,
            "confidence": round(confidence, 2), "source": source,
            "first_seen": today, "last_seen": today, "mentions": 1,
            "pinned": False,
        })
        added += 1

    out["facts"] = _prune(facts)
    out["updated_at"] = today
    return out, added


def _prune(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the most load-bearing `MAX_FACTS`: pinned first, then by how often the
    fact has come up and how confident it is, freshest breaking ties."""
    def score(fact: Dict[str, Any]) -> Tuple[int, float, str]:
        return (1 if fact.get("pinned") else 0,
                float(fact.get("confidence") or 0) + int(fact.get("mentions") or 1),
                str(fact.get("last_seen") or ""))
    return sorted(facts, key=score, reverse=True)[:MAX_FACTS]


def add_manual(memory: Optional[Dict[str, Any]], *, kind: str, fact: str,
               today: str) -> Dict[str, Any]:
    """A fact the user typed themselves. Pinned, because they said it on purpose."""
    merged, _ = merge(memory, [{"type": kind, "fact": fact, "confidence": 1.0}],
                      today=today, source="user")
    target = fact_id(fact)
    for entry in merged.get("facts", []):
        if entry.get("id") == target or _same_meaning(entry.get("fact", ""), fact):
            entry["pinned"] = True
            entry["source"] = "user"
            entry["confidence"] = 1.0
    return merged


def remove(memory: Optional[Dict[str, Any]], fact_id_or_text: str) -> Dict[str, Any]:
    out = dict(memory or empty())
    target = str(fact_id_or_text or "")
    out["facts"] = [f for f in (out.get("facts") or [])
                    if isinstance(f, dict)
                    and f.get("id") != target
                    and f.get("fact") != target]
    return out


def for_prompt(memory: Optional[Dict[str, Any]], limit: int = 20
               ) -> List[Dict[str, Any]]:
    """The memory as the model sees it: just the type and the sentence, ranked. The
    bookkeeping (ids, counts, dates) is ours and would only be noise in a prompt."""
    facts = _prune([f for f in ((memory or {}).get("facts") or [])
                    if isinstance(f, dict) and f.get("fact")])
    return [{"type": f.get("type", "context"), "fact": f["fact"]}
            for f in facts[:limit]]
