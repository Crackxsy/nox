"""`RelevanceClassifier`: deterministic 0-1 relevance score and `addressed_to_nox` flag for every
inbound `twitch.chat_message` (ST-11-04, Spec v0.2 Stream Bot §3.3). No LLM call - the classifier
only decides whether *something downstream* should look closer, never what to say.

Signals: a bot-name mention, the message being a question, a `!command`, a greeting, and a
per-viewer cooldown that damps score for a viewer who was just scored (keeps a chatty viewer from
repeatedly registering as "addressed" for ordinary chatter; an explicit `!command` always bypasses
the cooldown damping).
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
_COMMAND_SCORE = 0.5
_GREETING_SCORE = 0.2
_COOLDOWN_DAMPING = 0.3
_ADDRESSED_THRESHOLD = 0.5


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
        mentions_bot = any(name in lowered for name in self._bot_names)
        is_command = lowered.startswith("!")
        is_question = lowered.endswith("?") or any(
            lowered.startswith(w + " ") for w in _QUESTION_WORDS
        )
        is_greeting = any(lowered.startswith(g) for g in _GREETINGS)

        score = 0.0
        if mentions_bot:
            score += _MENTION_SCORE
        if is_question:
            score += _QUESTION_SCORE
        if is_command:
            score += _COMMAND_SCORE
        if is_greeting:
            score += _GREETING_SCORE

        now = self._clock()
        last = self._last_seen.get(viewer_id)
        on_cooldown = last is not None and (now - last) < self._cooldown_s
        if on_cooldown and not is_command:
            score *= _COOLDOWN_DAMPING
        self._last_seen[viewer_id] = now

        score = max(0.0, min(1.0, score))
        addressed = mentions_bot or is_command or score >= _ADDRESSED_THRESHOLD
        return score, addressed
