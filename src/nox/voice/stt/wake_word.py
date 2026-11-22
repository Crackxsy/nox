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
_WORD = re.compile(r"\S+")

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


#: Letters from other scripts (and Latin small capitals) that look like a Latin letter, in both
#: cases - an upper-case "ΝΟΧ" looks like "NOX" though its lower case "νοχ" does not. Whatever
#: is compared here is a transcript, and a transcript with lookalikes in it should still match
#: the wake word and above all the kill phrase. (What Nox itself says on a speaker is stripped
#: of every non-Latin letter instead, `nox.node.wire.speech.latin_only`: a speech engine reads
#: letters by their sound, not by their look.) Deliberately small, not a full confusables table.
_LOOKALIKES = {
    # Cyrillic, lower case
    "а": "a", "в": "b", "е": "e", "ё": "e", "г": "r", "һ": "h", "і": "i", "ї": "i",
    "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o", "п": "n", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ь": "b",
    "ү": "y", "ҳ": "x", "ӏ": "l",
    # Cyrillic, upper case
    "А": "a", "В": "b", "Е": "e", "Ё": "e", "К": "k", "М": "m", "Н": "h", "О": "o",
    "Р": "p", "С": "c", "Т": "t", "У": "y", "Х": "x", "Ѕ": "s", "І": "i", "Ј": "j",
    "Ү": "y", "Һ": "h", "Ԛ": "q", "Ԝ": "w", "Ӏ": "l",
    # Greek, lower case
    "α": "a", "β": "b", "γ": "y", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "u",
    "ν": "v", "ο": "o", "ρ": "p", "σ": "o", "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    "ϲ": "c", "ϳ": "j",
    # Greek, upper case
    "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m",
    "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x", "Ϲ": "c", "Ϳ": "j",
    # Armenian
    "օ": "o", "ո": "n", "ս": "u", "հ": "h", "ց": "g", "Օ": "o", "Ս": "u",
    # Latin small capitals and other Latin lookalikes NFKC leaves alone
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ɢ": "g", "ʜ": "h", "ɪ": "i",
    "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r",
    "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "ı": "i",
    "ɑ": "a", "ɡ": "g", "ɩ": "i", "ʋ": "u",
}  # fmt: skip
_CONFUSABLES = str.maketrans(_LOOKALIKES)

#: Unicode categories dropped before anything is compared: format characters (zero-width
#: joiners, bidi controls) and combining marks left over after NFKC (variation selectors, the
#: combining grapheme joiner) - each could split a word a speech engine still reads as one.
_INVISIBLE = frozenset({"Cf", "Mn", "Me"})


def fold_confusables(text: str) -> str:
    """`text` with compatibility forms unified (NFKC: fullwidth, ligatures, styled letters),
    invisible characters removed, the lookalikes above replaced by the Latin letter they
    imitate - in the original case, so upper-case lookalikes are caught too - and then in lower
    case."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(c for c in folded if unicodedata.category(c) not in _INVISIBLE)
    return folded.translate(_CONFUSABLES).lower()


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
    #: Text after the wake word, exactly as transcribed: capitalisation, punctuation, digits
    #: ("Treffen mit Anna am 3. Mai um 18:30 Uhr?"). Names and numbers must reach the memory and the
    #: language model as Whisper wrote them.
    remainder: str


#: Characters between the wake word and the request that are not part of the request.
_SEPARATORS = " \t\r\n,.:;!?-\u2013\u2014"


class WakeWordMatcher:
    def __init__(self, name: str = "Nox") -> None:
        self.name = normalize(name) or "nox"

    def _tokens(self, text: str) -> list[str]:
        return normalize(text).split()

    def match(self, text: str) -> WakeMatch:
        # Every comparison token remembers the whitespace-separated word it came from, so the
        # request can be cut from the ORIGINAL text.
        words = list(_WORD.finditer(text))
        tokens: list[str] = []
        owner: list[int] = []
        for index, word in enumerate(words):
            for token in normalize(word.group()).split():
                tokens.append(token)
                owner.append(index)
        if not tokens:
            return WakeMatch(False, False, "")
        kill = self._has_kill_phrase(tokens)
        start = 0
        while start < len(tokens) and tokens[start] in GREETINGS and start < 2:
            start += 1
        addressed = start < len(tokens) and is_wake_token(tokens[start], name=self.name)
        if addressed:
            remainder = text[words[owner[start]].end() :].lstrip(_SEPARATORS)
        else:
            remainder = text.strip()
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
