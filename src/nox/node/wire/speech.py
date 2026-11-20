"""What may be said out loud on a node's speaker, decided the same way on both sides.

The core filters every sentence it sends to a node's voice satellite with these functions and the
node filters it again before it speaks: a speech engine reads letters by their sound, and a
node's own voice must never come back through a microphone as the wake word or a kill phrase.
Owns the text rules only; the speaker, the microphone and the satellite are
`nox.node.device.voice`.
"""

from __future__ import annotations

import unicodedata

from nox.voice.stt.wake_word import KILL_PHRASES, is_wake_token, normalize

__all__ = ["latin_only", "say_refusal", "says_a_command", "speakable"]

#: The last code point of the Latin blocks a German or English voice reads: Basic Latin, Latin-1
#: Supplement (ä ö ü ß é ...), Latin Extended-A and -B.
_LAST_LATIN = 0x024F


#: The only punctuation that is said (or rather, shapes how the rest is said).
_SPOKEN_PUNCTUATION = frozenset(".,;:!?'\"-–—()/%&+=@#")


def latin_only(text: str) -> str:
    """`text` as a German or English voice may read it out - an allowlist: after NFKC (fullwidth
    and many styled letters become plain ones) only letters and digits up to the end of Latin
    Extended-B, whitespace (as single spaces) and `_SPOKEN_PUNCTUATION` are kept; everything
    else - letters of other scripts, letterlike and enclosed forms, mathematical alphanumerics
    NFKC leaves alone, symbols, emoji, invisible characters - is dropped. A speech engine reads
    letters by their sound, not their look (Cyrillic "ноx" is read "nox"), and may read out a
    symbol's name. The wake-word and kill-phrase checks and the speech engine all get exactly
    this result, so none of them can be shown one spelling and the others another."""

    kept: list[str] = []
    for c in unicodedata.normalize("NFKC", text):
        if c.isspace():
            kept.append(" ")
        elif c in _SPOKEN_PUNCTUATION:
            kept.append(c)
        elif ord(c) <= _LAST_LATIN and unicodedata.category(c) in ("Lu", "Ll", "Lt", "Lo", "Nd"):
            kept.append(c)
        # anything else is dropped without a gap: an invisible character inside a word must not
        # split it in two for the checks while the engine still reads it as one
    return " ".join("".join(kept).split())


def _plain(text: str) -> list[str]:
    """Words, case- and diacritics-insensitive ("NÖX" -> "nox")."""

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return normalize(stripped).split()


def says_a_command(text: str, wake_word: str = "Nox") -> bool:
    """Whether `text`, spoken here, could address Nox: the wake word (or a spelling of it)
    anywhere, or a kill phrase - checked on what would really be said (`latin_only`). Nox never
    says that itself (`voice.say`): its own voice would come back through a microphone as a
    command."""

    names = {w for w in _plain(wake_word)} or {"nox"}
    words = _plain(latin_only(text))
    if any(is_wake_token(word, name=name) for word in words for name in names):
        return True
    return any(
        tuple(words[i : i + len(phrase)]) == phrase
        for phrase in KILL_PHRASES
        for i in range(len(words))
    )


def speakable(text: str, wake_word: str = "Nox") -> str | None:
    """`text` as it may be spoken on a speaker a microphone can hear: only its
    Latin letters (`latin_only`), the wake word (any spelling of it) as "ich", and None - not
    spoken at all - for a sentence with a kill phrase or with nothing left to say. Applied to
    every sentence the core sends a node, and again on the node."""

    text = latin_only(text)
    if not any(c.isalnum() for c in text):
        return None
    words = _plain(text)
    if any(
        tuple(words[i : i + len(phrase)]) == phrase
        for phrase in KILL_PHRASES
        for i in range(len(words))
    ):
        return None
    names = set(_plain(wake_word)) or {"nox"}
    out: list[str] = []
    for token in text.split():
        plain = _plain(token)
        if plain and all(any(is_wake_token(w, name=n) for n in names) for w in plain):
            tail = token[len(token.rstrip(".,;:!?…")) :]
            out.append("ich" + tail)
        else:
            out.append(token)
    rewritten = " ".join(out)
    return None if says_a_command(rewritten, wake_word) else rewritten


def say_refusal(text: str, wake_word: str = "Nox") -> str | None:
    """Why `voice.say` may not say `text`, or None: never the wake word or a kill phrase (its own
    voice would come back through a microphone as a command), never nothing."""
    if says_a_command(text, wake_word):
        return "voice_say_command_words"
    if not any(c.isalnum() for c in latin_only(text)):
        return "voice_say_nothing_to_say"
    return None
