"""Wake-word and local kill-phrase matching on transcripts (the voice path of the kill switch).

Whisper spells the name in many ways ("Nox", "Knox", "Nocks", "Nocs"); the matcher normalizes the
text, skips a leading greeting ("hey", "hallo", "ok") and fuzzy-matches the first token.

The kill phrase ("Nox Notaus" / "Nox emergency stop") is detected here so the worker can stop
everything directly - it never travels through the LLM. Because it engages the kill switch, the
matcher is deliberately narrow about *where* the phrase may stand and generous about *how* it may
be spelled:

* The phrase must be the whole utterance, apart from greetings, filler words ("äh", "bitte",
  "jetzt") and punctuation, and it must start with the wake word. "Sag einfach Nox Notaus", "Was
  macht der Notaus-Knopf" and "Nox, Notaus-Knopf erklären" talk *about* the phrase and never
  count.
* The kill word tolerates what Whisper makes of it: "Nottaus", "Notauss", "Nota aus", "Not-Aus",
  and the English renderings a short German clip gets when language detection picks English
  ("Knox, not out", "Nox not house").

`mentions_kill_phrase` is the loose counterpart: does a text contain the kill word anywhere. The
pipeline uses it on Nox's *own* speech, so a reply that quotes the phrase cannot trigger the kill
switch through the microphone.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

GREETINGS = frozenset({"hey", "he", "hallo", "hi", "ok", "okay", "ey", "yo", "also", "so"})
WAKE_SPELLINGS = frozenset(
    {
        "nox",
        "knox",
        "nocks",
        "nocs",
        "noks",
        "nogs",
        "nux",
        "nuks",
        "nocx",
        "knocks",
        "noxx",
        "nochs",
    }
)
#: Words that may surround the kill phrase without turning it into a sentence about it.
FILLERS = frozenset(
    {
        "äh",
        "ähm",
        "ähh",
        "öhm",
        "aeh",
        "aehm",
        "eh",
        "ehm",
        "em",
        "hm",
        "hmm",
        "uh",
        "uhm",
        "um",
        "bitte",
        "please",
        "jetzt",
        "sofort",
        "now",
        "mal",
        "schnell",
    }
)
#: The kill word as token sequences: the German phrase and its hyphen/space variants, the English
#: phrase, and what Whisper writes when it hears "Notaus" as English.
KILL_PHRASES: tuple[tuple[str, ...], ...] = (
    ("notaus",),
    ("not", "aus"),
    ("nothalt",),
    ("not", "halt"),
    ("emergency", "stop"),
    ("emergency", "shutdown"),
    ("not", "house"),
    ("not", "a", "house"),
    ("not", "out"),
    ("note", "out"),
    ("knot", "out"),
    ("nod", "out"),
)
#: Single-word spellings of the kill word are compared against these with difflib.
_KILL_WORDS = ("notaus", "nothalt")
_KILL_WORD_RATIO = 0.8
#: A kill word Whisper split in two ("nota aus", "noth aus") is re-joined when the first half is
#: this short; longer first halves are ordinary words ("notar", "notiz").
_SPLIT_HEAD_MAX = 4


def normalize(text: str) -> str:
    return _SPACES.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def is_wake_token(token: str, *, name: str = "nox", threshold: float = 0.75) -> bool:
    if token in WAKE_SPELLINGS or token == name:
        return True
    if not (2 <= len(token) <= 6):
        return False
    return difflib.SequenceMatcher(None, token, name).ratio() >= threshold


def _is_kill_word(token: str) -> bool:
    if not (5 <= len(token) <= 9):
        return False
    return any(
        difflib.SequenceMatcher(None, token, word).ratio() >= _KILL_WORD_RATIO
        for word in _KILL_WORDS
    )


def kill_phrase_length(tokens: list[str], start: int) -> int:
    """How many tokens from `start` spell the kill word; 0 when they do not."""
    for phrase in KILL_PHRASES:
        if tuple(tokens[start : start + len(phrase)]) == phrase:
            return len(phrase)
    if start >= len(tokens):
        return 0
    head = tokens[start]
    if _is_kill_word(head):
        return 1
    if start + 1 < len(tokens) and len(head) <= _SPLIT_HEAD_MAX:
        if _is_kill_word(head + tokens[start + 1]):
            return 2
    return 0


def mentions_kill_phrase(text: str) -> bool:
    """True when the kill word appears anywhere in `text` - the loose check for Nox's own speech."""
    tokens = normalize(text).split()
    return any(kill_phrase_length(tokens, i) for i in range(len(tokens)))


@dataclass(frozen=True)
class WakeMatch:
    addressed: bool  # utterance starts with the wake word (after optional greeting)
    kill: bool  # the whole utterance is the kill phrase ("Nox Notaus", modulo fillers)
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
        kill = self.is_kill_utterance(tokens)
        start = 0
        while start < len(tokens) and tokens[start] in GREETINGS and start < 2:
            start += 1
        addressed = start < len(tokens) and is_wake_token(tokens[start], name=self.name)
        if addressed:
            remainder = " ".join(tokens[start + 1 :])
        else:
            remainder = " ".join(tokens)
        return WakeMatch(addressed=addressed, kill=kill, remainder=remainder)

    def _is_wake(self, token: str) -> bool:
        return is_wake_token(token, name=self.name)

    def _glued_kill(self, token: str) -> bool:
        """ "noxnotaus": Whisper occasionally writes the phrase as one word."""
        for spelling in (*WAKE_SPELLINGS, self.name):
            if token.startswith(spelling) and len(token) > len(spelling):
                rest = token[len(spelling) :]
                if kill_phrase_length([rest], 0):
                    return True
        return False

    def is_kill_utterance(self, tokens: list[str]) -> bool:
        """[greetings/fillers] wake-word [fillers] kill-word [fillers | kill-word | wake-word]."""
        i = 0
        while i < len(tokens) and (tokens[i] in GREETINGS or tokens[i] in FILLERS):
            i += 1
        if i >= len(tokens):
            return False
        if self._glued_kill(tokens[i]):
            i += 1
        else:
            if not self._is_wake(tokens[i]):
                return False
            while i < len(tokens) and (self._is_wake(tokens[i]) or tokens[i] in FILLERS):
                i += 1
            length = kill_phrase_length(tokens, i)
            if not length:
                return False
            i += length
        while i < len(tokens):
            if tokens[i] in FILLERS or self._is_wake(tokens[i]):
                i += 1
                continue
            length = kill_phrase_length(tokens, i)
            if not length:
                return False
            i += length
        return True
