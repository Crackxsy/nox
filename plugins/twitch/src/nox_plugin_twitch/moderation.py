"""`ModerationGate`: deterministic pre-send check for every outgoing chat message (ST-11-04, Spec
v0.2 Stream Bot §3.4). Runs on *Nox's own* text - `twitch.chat.send` and every built-in command
reply go through it - never on inbound viewer chat (that is untrusted data, evaluated, not acted on
as instructions, FR-9.16).

The hard exclusions below are independent of chat mood/insult level and cannot be relaxed by
config, a profile, or a "mach einfach" grant (FR-9.9): hate speech against a protected group,
sexual content about a viewer, doxxing patterns, illness/death jokes, and insults aimed at a named
viewer. `blocklist` is additional, configurable, non-exhaustive terms on top of those.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

_PROTECTED_GROUPS = (
    "jews",
    "muslims",
    "christians",
    "black people",
    "white people",
    "asians",
    "gay people",
    "lesbians",
    "trans people",
    "immigrants",
    "disabled people",
    "juden",
    "muslime",
    "schwarze",
    "behinderte",
)
_HATE_PHRASES = (
    "should be killed",
    "should die",
    "are subhuman",
    "deserve to die",
    "should be exterminated",
    "are vermin",
    "sollen sterben",
    "sind untermenschen",
)

_SEXUAL_WORDS = (
    "nude",
    "nudes",
    "naked",
    "horny",
    "send pics",
    "dick pic",
    "nacktbild",
    "nacktbilder",
)
_SECOND_PERSON_MARKERS = (" you ", "you're", " du ", " dich ", " dir ", "@")

_DOXX_PHRASES = (
    "here's their address",
    "here is their address",
    "home address is",
    "real name is",
    "ihre adresse ist",
    "seine adresse ist",
)
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
_STREET_RE = re.compile(
    r"\b\d{1,5}\s+\w+\s+(street|st|avenue|ave|road|rd|drive|dr|strasse|straße)\b", re.IGNORECASE
)

_DEATH_ILLNESS_PHRASES = (
    "kys",
    "kill yourself",
    "hope you die",
    "die of cancer",
    "cancer joke",
    "bring dich um",
    "häng dich auf",
)

_INSULT_WORDS = (
    "idiot",
    "stupid",
    "loser",
    "trash",
    "garbage",
    "ugly",
    "worthless",
    "pathetic",
    "dumm",
    "wertlos",
)
_MENTION_RE = re.compile(r"@\w+")


def _contains_any(text: str, words: Sequence[str]) -> bool:
    return any(word in text for word in words)


def _is_hate_speech(text: str) -> bool:
    return _contains_any(text, _PROTECTED_GROUPS) and _contains_any(text, _HATE_PHRASES)


def _is_sexual_about_viewer(text: str) -> bool:
    padded = f" {text} "
    return _contains_any(text, _SEXUAL_WORDS) and any(m in padded for m in _SECOND_PERSON_MARKERS)


def _is_doxxing(text: str) -> bool:
    return (
        _contains_any(text, _DOXX_PHRASES)
        or _PHONE_RE.search(text) is not None
        or _STREET_RE.search(text) is not None
    )


def _is_illness_death_joke(text: str) -> bool:
    return _contains_any(text, _DEATH_ILLNESS_PHRASES)


def _is_insult_at_viewer(text: str) -> bool:
    return _MENTION_RE.search(text) is not None and _contains_any(text, _INSULT_WORDS)


_HARD_EXCLUSIONS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("hate_speech_protected_group", _is_hate_speech),
    ("sexual_content_about_viewer", _is_sexual_about_viewer),
    ("doxxing", _is_doxxing),
    ("illness_or_death_joke", _is_illness_death_joke),
    ("insult_aimed_at_viewer", _is_insult_at_viewer),
)


class ModerationGate:
    """`check(text) -> (ok, reason)`. Deterministic, no LLM call - a hard-list hit is never
    something an AI call gets to reinterpret (FR-9.9)."""

    def __init__(self, blocklist: Sequence[str] = ()) -> None:
        self._blocklist = [b.strip().lower() for b in blocklist if b.strip()]

    def check(self, text: str) -> tuple[bool, str]:
        lowered = text.lower()
        for reason, predicate in _HARD_EXCLUSIONS:
            if predicate(lowered):
                return False, reason
        for word in self._blocklist:
            if word in lowered:
                return False, f"blocklist:{word}"
        return True, ""
