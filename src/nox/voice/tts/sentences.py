"""Sentence splitting for streaming TTS (FR-5.4): synthesize per sentence, speak when ready."""

from __future__ import annotations

import re

_ABBREVIATIONS = frozenset(
    {
        "z.b",
        "d.h",
        "u.a",
        "usw",
        "bzw",
        "ca",
        "dr",
        "prof",
        "nr",
        "hr",
        "fr",
        "evtl",
        "ggf",
        "vgl",
        "etc",
        "mr",
        "mrs",
        "ms",
        "vs",
        "e.g",
        "i.e",
        "st",
        "inkl",
        "min",
        "max",
        "sog",
    }
)
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")
_TRAILING_PUNCT = re.compile(r"[.!?…]+$")


def _ends_with_abbreviation(piece: str) -> bool:
    word = piece.rstrip(".!?…").rsplit(" ", 1)[-1].lower()
    return word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())


def split_sentences(text: str, *, min_chars: int = 4) -> list[str]:
    """Split on sentence punctuation followed by whitespace, and on newlines.

    Keeps decimal numbers ("3.5"), common DE/EN abbreviations and single-letter initials together;
    very short fragments are merged into their predecessor so a lone "Ok." does not become a chunk
    of its own with an audible gap after it.
    """
    pieces = [p.strip() for p in _BOUNDARY.split(text) if p and p.strip()]
    merged: list[str] = []
    for piece in pieces:
        if merged and (_ends_with_abbreviation(merged[-1]) or len(merged[-1]) < min_chars):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    if len(merged) > 1 and len(_TRAILING_PUNCT.sub("", merged[-1])) < min_chars:
        last = merged.pop()
        merged[-1] = f"{merged[-1]} {last}"
    return merged
