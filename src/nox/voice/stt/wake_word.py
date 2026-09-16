"""Wake-word and local kill-phrase matching on transcripts (FR-5.1, FR-5.2, kill switch D139).

Whisper spells the name in many ways ("Nox", "Knox", "Nocks", "Nocs"); the matcher normalizes the
text, skips a leading greeting ("hey", "hallo", "ok") and fuzzy-matches the first token. The kill
phrase ("Nox Notaus" / "Nox emergency stop") is detected here so the worker can send `security.kill`
directly - it never travels through the LLM.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

GREETINGS = frozenset({"hey", "he", "hallo", "hi", "ok", "okay", "ey", "yo", "also", "so"})
WAKE_SPELLINGS = frozenset(
    {"nox", "knox", "nocks", "nocs", "noks", "nogs", "nux", "nocx", "knocks", "noxx", "nochs"}
)
KILL_PHRASES: tuple[tuple[str, ...], ...] = (
    ("notaus",),
    ("not", "aus"),
    ("nothalt",),
    ("not", "halt"),
    ("emergency", "stop"),
    ("emergency", "shutdown"),
)


def normalize(text: str) -> str:
    return _SPACES.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def is_wake_token(token: str, *, name: str = "nox", threshold: float = 0.75) -> bool:
    if token in WAKE_SPELLINGS or token == name:
        return True
    if not (2 <= len(token) <= 6):
        return False
    return difflib.SequenceMatcher(None, token, name).ratio() >= threshold


@dataclass(frozen=True)
class WakeMatch:
    addressed: bool  # utterance starts with the wake word (after optional greeting)
    kill: bool  # kill phrase present anywhere ("Nox Notaus" ...)
    remainder: str  # text after the wake word (original casing preserved best-effort)


class WakeWordMatcher:
    def __init__(self, name: str = "Nox") -> None:
        self.name = normalize(name) or "nox"

    def _tokens(self, text: str) -> list[str]:
        return normalize(text).split()

    def match(self, text: str) -> WakeMatch:
        tokens = self._tokens(text)
        if not tokens:
            return WakeMatch(False, False, "")
        kill = self._has_kill_phrase(tokens)
        start = 0
        while start < len(tokens) and tokens[start] in GREETINGS and start < 2:
            start += 1
        addressed = start < len(tokens) and is_wake_token(tokens[start], name=self.name)
        if addressed:
            remainder = " ".join(tokens[start + 1 :])
        else:
            remainder = " ".join(tokens)
        return WakeMatch(addressed=addressed, kill=kill, remainder=remainder)

    def _has_kill_phrase(self, tokens: list[str]) -> bool:
        for i, tok in enumerate(tokens):
            if not is_wake_token(tok, name=self.name):
                continue
            tail = tokens[i + 1 : i + 4]
            for phrase in KILL_PHRASES:
                if tuple(tail[: len(phrase)]) == phrase:
                    return True
        return False
