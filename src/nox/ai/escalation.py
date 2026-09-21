"""When a chat turn is too hard for the small local model and should take the reasoning chain.

A 3B model answers "hallo" and "fasse das kurz zusammen" well and answers "warum bricht mein
Backup seit dem Update ab und was sind die drei plausibelsten Ursachen" badly. Routing the second
kind to :data:`AiRole.REASON` puts the configured reasoner (Claude Code by default) in front of
the chain instead of behind it.

The trigger is deliberately mechanical, so it can be tested and argued with:

1. an explicit marker in the utterance ("denk nach", "think hard") - the user asked for it; or
2. a long question *and* no relevant memory to ground it with. Length alone is not enough (a long
   dictated note is not a hard question) and missing memory alone is not enough (most chit-chat
   has no memory behind it either) - it takes both.

Escalation never overrides privacy: the router still filters cloud providers by privacy mode and
profile, so an escalated turn in OFFLINE mode simply lands on the local model again, with the
router's own note saying why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nox.ai.base import AiRole

#: Words from which a question is long enough to be a candidate for the reasoning chain. Measured
#: against the benchmark prompts: the chit-chat and fact prompts are 2-9 words, the two prompts a
#: 3B model visibly struggles with are 26 and 31.
LONG_QUESTION_WORDS = 22

#: Cosine similarity below which retrieved memory does not count as grounding for the question.
#: It matches `nox.memory.retrieval.DEFAULT_MIN_SCORE`: in the shipped configuration that makes
#: "grounded" mean "retrieval found something it was willing to put in the prompt". It stays a
#: separate number because the retrieval floor is configurable and this one is a routing decision,
#: not a prompt-budget decision.
MIN_GROUNDING_RELEVANCE = 0.75

#: Words allowed between "denk" and "nach", so "denk bitte gründlich nach" matches while "denk
#: daran, das Backup nach dem Update zu prüfen" does not.
_FILLER = r"(?:mal|bitte|kurz|genau|gr(?:ü|ue)ndlich|scharf|in\s+ruhe|noch\s+mal)"

_EXPLICIT_MARKERS = re.compile(
    rf"\bdenk(?:e)?\s+(?:{_FILLER}\s+){{0,3}}nach\b"
    r"|\b(?:ü|ue)berleg(?:e|en)?\s+(?:dir\s+)?(?:das\s+)?"
    r"(?:genau|gr(?:ü|ue)ndlich|in\s+ruhe)\b"
    r"|\bthink\s+(?:hard|carefully|it\s+through)\b"
    r"|\btake\s+your\s+time\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """The role the turn is sent with, and why. `reason` is empty for an ordinary chat turn."""

    role: AiRole
    reason: str = ""


@dataclass(slots=True)
class EscalationPolicy:
    """Decides between the chat chain and the reasoning chain (module docstring for the rule)."""

    enabled: bool = True
    long_question_words: int = LONG_QUESTION_WORDS
    min_grounding_relevance: float = MIN_GROUNDING_RELEVANCE

    def decide(self, text: str, *, relevance: float) -> EscalationDecision:
        """Route this turn. `relevance` is the best semantic score of the memory retrieved for it,
        or 0.0 when nothing was retrieved."""
        if not self.enabled:
            return EscalationDecision(role=AiRole.CHAT)
        if _EXPLICIT_MARKERS.search(text):
            return EscalationDecision(role=AiRole.REASON, reason="explicit_marker")
        words = len(text.split())
        if words >= self.long_question_words and relevance < self.min_grounding_relevance:
            return EscalationDecision(
                role=AiRole.REASON,
                reason=(
                    f"long_question_without_grounding ({words} words, relevance {relevance:.2f})"
                ),
            )
        return EscalationDecision(role=AiRole.CHAT)


__all__ = [
    "LONG_QUESTION_WORDS",
    "MIN_GROUNDING_RELEVANCE",
    "EscalationDecision",
    "EscalationPolicy",
]
