"""`RelevanceClassifier`: deterministic 0-1 relevance score and `addressed_to_nox` flag for every
inbound `twitch.chat_message` (Stream Bot). No LLM call - the classifier only decides whether
*something downstream* should look closer, never what to say.

Signals: a bot-name mention (a whole word, so "equinox" is not one), a reply to one of the bot's own
messages, the message being a question, a greeting, and a per-viewer cooldown that damps score for a
viewer who was just scored (keeps a chatty viewer from repeatedly registering as "addressed" for
ordinary chatter).

A message that starts with `!` is a command, and a command belongs to whoever owns it: the bot's
own commands (`!rps`, `!funken`, ...) are answered by the plugin, another bot's (`!lurk`,
`!discord`, ...) is none of Nox's business. Only `!nox ...` (or `!<bot login>`) is a request to
Nox itself. Every other command scores 0 and is never "addressed", whatever else the line says, so
nothing downstream can answer it with a language-model reply on top of the real command's answer.
"""

from __future__ import annotations

import re
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

#: `!word` at the start of a line; `! word` (a space after the bang) is no command anywhere.
_COMMAND_WORD_RE = re.compile(r"!(\S+)")

_MENTION_SCORE = 0.6
_QUESTION_SCORE = 0.2
_COMMAND_SCORE = 0.5
_REPLY_SCORE = 0.6
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

    def classify(
        self,
        viewer_id: str,
        text: str,
        *,
        replied_to_bot: bool = False,
        bot_login: str = "",
    ) -> tuple[float, bool]:
        """Score one chat line. `replied_to_bot`: the viewer used Twitch's reply on a message of
        the bot's. `bot_login`: the bot account's own login, counted as one more name."""
        lowered = text.strip().lower()
        names = self._names(bot_login)
        if lowered.startswith("!") and not _is_request_to_bot(lowered, names):
            return 0.0, False
        is_command = lowered.startswith("!")
        mentions_bot = _mentions_any(lowered, names)
        is_question = lowered.endswith("?") or any(
            lowered.startswith(w + " ") for w in _QUESTION_WORDS
        )
        is_greeting = any(lowered.startswith(g) for g in _GREETINGS)

        score = 0.0
        if mentions_bot:
            score += _MENTION_SCORE
        if replied_to_bot:
            score += _REPLY_SCORE
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
        addressed = mentions_bot or replied_to_bot or is_command or score >= _ADDRESSED_THRESHOLD
        return score, addressed

    def _names(self, bot_login: str) -> list[str]:
        login = bot_login.strip().lower().lstrip("@")
        return [*self._bot_names, login] if login else self._bot_names


def _mentions_any(lowered: str, names: Sequence[str]) -> bool:
    """A name as a whole word: "nox" in "hey @nox" or "nox, wie gehts", not in "equinox"."""
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered) for name in names)


def _is_request_to_bot(lowered: str, names: Sequence[str]) -> bool:
    """`!nox <question>`: the one command form that asks Nox itself."""
    match = _COMMAND_WORD_RE.match(lowered)
    return match is not None and match.group(1) in names
