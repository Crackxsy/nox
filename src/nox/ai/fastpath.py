"""Deterministic answers for the utterances a language model adds nothing to.

"Hallo", "danke", "wie spät ist es" cost a 3B model a full prompt evaluation and several seconds
of generation to produce something a table lookup produces in under a millisecond. This module is
that table, in front of the router.

The one rule it must never break: **it may not swallow a real question.** Every pattern is matched
against the *whole* normalised utterance (`re.fullmatch`), never searched inside it, and anything
longer than :data:`MAX_WORDS` words is refused before matching even starts. "Hallo" is a greeting;
"Hallo, kannst du mir beim Deployment helfen?" is a question and goes to the model. When in doubt
the model answers - a slow good answer beats a fast wrong one.

The answers are deliberately short and deliberately free of any claim the assistant cannot back
up: no invented mood, no invented availability.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from nox.ai.language import Language, detect_language, render_clock, render_date

#: Longest utterance the fast path will even look at. Every phrase it knows is at most five words;
#: the cap is the second line of defence behind `fullmatch` and the one the tests pin down.
MAX_WORDS = 6

#: Hour boundaries for the greeting wording (local time).
MORNING_UNTIL_HOUR = 11
EVENING_FROM_HOUR = 18

#: A leading or trailing form of address does not change the meaning of a phatic utterance. The
#: leading form needs a separator after it and the trailing one needs whitespace before it, so a
#: bare "Nox" survives normalisation and goes to the model rather than being erased into a
#: greeting.
_ADDRESS = re.compile(r"^nox[,!.\s]+|\s+nox$", re.IGNORECASE)
#: Apostrophes vanish ("what's" -> "whats"); other punctuation becomes a word break.
_APOSTROPHE = re.compile(r"['`´’]")
_PUNCTUATION = re.compile(r"[!?.…,;:\"]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FastAnswer:
    """A deterministic answer plus the intent that produced it (for logs and the dashboard)."""

    intent: str
    text: str


def _alt(*phrases: str) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(p) for p in phrases))


#: Intent patterns, checked in order. Each matches the *whole* normalised utterance.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "greeting",
        _alt(
            "hallo",
            "hallöchen",
            "hi",
            "hey",
            "huhu",
            "moin",
            "moin moin",
            "servus",
            "grüß dich",
            "guten morgen",
            "guten tag",
            "guten abend",
            "hello",
            "good morning",
            "good afternoon",
            "good evening",
        ),
    ),
    (
        "thanks",
        _alt(
            "danke",
            "danke dir",
            "dankeschön",
            "danke schön",
            "vielen dank",
            "besten dank",
            "tausend dank",
            "thanks",
            "thank you",
            "thanks a lot",
            "thx",
        ),
    ),
    (
        "farewell",
        _alt(
            "tschüss",
            "tschüs",
            "ciao",
            "bis später",
            "bis dann",
            "bis bald",
            "gute nacht",
            "machs gut",
            "bye",
            "goodbye",
            "good night",
            "see you",
            "see ya",
        ),
    ),
    (
        "time",
        _alt(
            "wie spät",
            "wie spät ist es",
            "wie spät ist es denn",
            "wie spät haben wir",
            "wie viel uhr ist es",
            "wieviel uhr ist es",
            "wie viel uhr haben wir",
            "uhrzeit",
            "sag mir die uhrzeit",
            "what time is it",
            "whats the time",
            "what is the time",
        ),
    ),
    (
        "date",
        _alt(
            "datum",
            "welches datum",
            "welches datum ist heute",
            "welches datum haben wir",
            "welches datum haben wir heute",
            "welcher tag ist heute",
            "welchen tag haben wir",
            "welchen tag haben wir heute",
            "what day is it",
            "what day is it today",
            "whats the date",
            "what is the date",
            "whats todays date",
        ),
    ),
    (
        "status",
        _alt(
            "wie geht es dir",
            "wie gehts dir",
            "wie geht es",
            "wie gehts",
            "alles klar",
            "alles gut",
            "alles ok",
            "alles okay",
            "how are you",
            "how are you doing",
            "hows it going",
            "how is it going",
            "you ok",
            "you okay",
            "are you ok",
            "are you okay",
        ),
    ),
]

_THANKS = {"de": "Gern.", "en": "Anytime."}
_FAREWELL = {"de": "Bis später.", "en": "See you."}
_STATUS = {
    "de": "Bei mir läuft alles stabil. Was steht an?",
    "en": "Everything is running fine here. What's up?",
}
_GREETING_TAIL = {"de": "Was steht an?", "en": "What's up?"}
_GREETING_HEAD = {
    "de": {"morning": "Guten Morgen.", "day": "Hallo.", "evening": "Guten Abend."},
    "en": {"morning": "Good morning.", "day": "Hello.", "evening": "Good evening."},
}


def normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace, drop a leading/trailing "Nox"."""
    folded = unicodedata.normalize("NFC", text).strip().lower()
    folded = _ADDRESS.sub("", folded).strip()
    folded = _PUNCTUATION.sub(" ", _APOSTROPHE.sub("", folded))
    return _WHITESPACE.sub(" ", folded).strip()


@dataclass(slots=True)
class FastPath:
    """Matches phatic utterances and answers them without a language model.

    `enabled=False` turns the whole path off, which is what the latency benchmark's baseline mode
    and any user who prefers the model's wording use. `clock` is injectable so the time and date
    answers are testable without freezing the system clock.
    """

    enabled: bool = True
    clock: Callable[[], datetime] = field(default_factory=lambda: _now)
    max_words: int = MAX_WORDS

    def match(self, text: str, language_hint: str | None = None) -> FastAnswer | None:
        """A deterministic answer for `text`, or `None` when the model should answer."""
        if not self.enabled:
            return None
        normalized = normalize(text)
        if not normalized or len(normalized.split()) > self.max_words:
            return None
        for intent, pattern in _PATTERNS:
            if pattern.fullmatch(normalized):
                language = detect_language(text, language_hint)
                return FastAnswer(intent=intent, text=self._answer(intent, language))
        return None

    def _answer(self, intent: str, language: Language) -> str:
        if intent == "greeting":
            return f"{self._greeting_head(language)} {_GREETING_TAIL[language]}"
        if intent == "thanks":
            return _THANKS[language]
        if intent == "farewell":
            return _FAREWELL[language]
        if intent == "status":
            return _STATUS[language]
        now = self.clock()
        if intent == "time":
            suffix = " Uhr." if language == "de" else "."
            prefix = "Es ist " if language == "de" else "It's "
            return f"{prefix}{render_clock(now, language)}{suffix}"
        prefix = "Heute ist " if language == "de" else "Today is "
        return f"{prefix}{render_date(now, language)}."

    def _greeting_head(self, language: Language) -> str:
        hour = self.clock().hour
        if hour < MORNING_UNTIL_HOUR:
            slot = "morning"
        elif hour >= EVENING_FROM_HOUR:
            slot = "evening"
        else:
            slot = "day"
        return _GREETING_HEAD[language][slot]


def _now() -> datetime:
    return datetime.now().astimezone()


__all__ = ["MAX_WORDS", "FastAnswer", "FastPath", "normalize"]
