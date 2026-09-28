"""`RelevanceClassifier`: deterministic 0-1 relevance score and `addressed_to_nox` flag for every
inbound `twitch.chat_message` (Stream Bot). No LLM call - the classifier only decides whether
*something downstream* should look closer, never what to say.

Signals: a bot-name mention, the message being a question, a greeting, and a per-viewer cooldown
that damps score for a viewer who was just scored (keeps a chatty viewer from repeatedly
registering as "addressed" for ordinary chatter).

A `!command` is never addressed to the conversation and scores zero: it is answered by a command
handler - one of this plugin's built-ins, the core's `!funken`, or another bot's `!discord` - and
an LLM reply on top of that would answer every `!rps rock` twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

_QUESTION_WORDS = (
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "wer",
    "was",
    "wann",
    "wo",
    "warum",
    "wie",
)
_GREETINGS = ("hi", "hello", "hey", "hallo", "servus", "moin")

_MENTION_SCORE = 0.6
_QUESTION_SCORE = 0.2
_GREETING_SCORE = 0.2
_COOLDOWN_DAMPING = 0.3
_ADDRESSED_THRESHOLD = 0.5


def is_chat_command(text: str) -> bool:
    """`!rps rock`, `!help`: a command for a bot, not a line of conversation."""
    return text.lstrip().startswith("!")


class RelevanceClassifier:
    def __init__(
        self,
        bot_names: Sequence[str] = ("nox",),
        *,
        cooldown_s: float = 20.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._bot_names = [n.strip().lower() for n in bot_names if n.strip()] or ["nox"]
        self._cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self._last_seen: dict[str, float] = {}

    def classify(self, viewer_id: str, text: str) -> tuple[float, bool]:
        lowered = text.strip().lower()
        if is_chat_command(lowered):
            return 0.0, False
        mentions_bot = any(name in lowered for name in self._bot_names)
        is_question = lowered.endswith("?") or any(
            lowered.startswith(w + " ") for w in _QUESTION_WORDS
        )
        is_greeting = any(lowered.startswith(g) for g in _GREETINGS)

        score = 0.0
        if mentions_bot:
            score += _MENTION_SCORE
        if is_question:
            score += _QUESTION_SCORE
        if is_greeting:
            score += _GREETING_SCORE

        now = self._clock()
        last = self._last_seen.get(viewer_id)
        on_cooldown = last is not None and (now - last) < self._cooldown_s
        if on_cooldown:
            score *= _COOLDOWN_DAMPING
        self._last_seen[viewer_id] = now

        score = max(0.0, min(1.0, score))
        addressed = mentions_bot or score >= _ADDRESSED_THRESHOLD
        return score, addressed
