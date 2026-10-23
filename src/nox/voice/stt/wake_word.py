"""Wake-word and local kill-phrase matching on transcripts (the spoken kill switch).

Whisper spells the name in many ways ("Nox", "Knox", "Nocks", "Nocs"); the matcher normalizes the
text, skips a leading greeting ("hey", "hallo", "ok") and fuzzy-matches the first token. The kill
phrase ("Nox Notaus" / "Nox emergency stop") is detected here so the worker can send
`security.kill` directly - it never travels through the LLM.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
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


#: Letters from other scripts (and Latin small capitals) that look like a Latin letter. A text Nox
#: is told to say could spell its own name or "Notaus" with them ("Nох" with Cyrillic о and х):
#: invisible to a plain comparison, but a speech engine reads it out as the name, and the
#: microphone hears a command. Folded before anything is compared. Deliberately small - the
#: lookalikes of the Latin alphabet in the scripts a German or English text is likely to borrow
#: them from - not a full confusables table.
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a", "в": "b", "е": "e", "ё": "e", "г": "r", "һ": "h", "і": "i", "ї": "i",
        "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o", "п": "n", "р": "p", "с": "c",
        "т": "t", "у": "y", "х": "x", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ь": "b",
        "ү": "y", "ҳ": "x", "ӏ": "l",
        # Greek
        "α": "a", "β": "b", "γ": "y", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "u",
        "ν": "v", "ο": "o", "ρ": "p", "σ": "o", "τ": "t", "υ": "u", "χ": "x", "ω": "w",
        "ϲ": "c", "ϳ": "j",
        # Armenian
        "օ": "o", "ո": "n", "ս": "u", "հ": "h", "ց": "g",
        # Latin small capitals and other Latin lookalikes NFKC leaves alone
        "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g", "ʜ": "h", "ɪ": "i",
        "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r",
        "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "ı": "i",
        "ɑ": "a", "ɡ": "g", "ɩ": "i", "ʋ": "u",
    }
)  # fmt: skip


def fold_confusables(text: str) -> str:
    """`text` in lower case with compatibility forms unified (NFKC: fullwidth, ligatures, styled
    letters), invisible format characters (zero-width joiners, bidi controls) removed - so they
    cannot split a word a speech engine still reads as one - and the lookalikes above replaced
    by the Latin letter they imitate."""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return folded.translate(_CONFUSABLES)


def normalize(text: str) -> str:
    return _SPACES.sub(" ", _PUNCT.sub(" ", fold_confusables(text))).strip()


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
