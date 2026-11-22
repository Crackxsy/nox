"""Sentence splitting: the one place that decides where a sentence ends.

Used twice, so that both cut at the same places: by the orchestrator, which hands a streamed answer
to the voice worker one sentence at a time (`split_stream`: what is complete so far, and the rest),
and by the engines, which synthesise a request sentence by sentence (`split_sentences`). Every cut
is a pause and a falling voice, so a cut in the wrong place is heard: "am 9. Mai", "z. B.",
"Dr. Meier" or "ca. 5" must stay in one piece.

A full stop is a sentence end unless the word in front of it is an abbreviation, a single letter
(an initial), or a number that is an ordinal or a date ("am 9. Mai", "am 22.11. um fünf", a list
marker such as "1." or "Schritt 2."). Whether a number is an ordinal depends on the word after it,
which may not have arrived yet in a stream: then the sentence is held back until it has (and
flushed when the stream ends). `!`, `?` and an ellipsis always end a sentence; a closing quote or
bracket after the mark stays with the sentence; a line break always ends one.
"""

from __future__ import annotations

import re

_MONTHS = frozenset(
    {
        "januar",
        "jänner",
        "februar",
        "märz",
        "april",
        "mai",
        "juni",
        "juli",
        "august",
        "september",
        "oktober",
        "november",
        "dezember",
        "january",
        "february",
        "march",
        "june",
        "july",
        "october",
        "december",
    }
)
#: A number in front of one of these is a list or section number, not the end of a sentence.
_LIST_WORDS = frozenset(
    {
        "schritt",
        "punkt",
        "nummer",
        "kapitel",
        "abschnitt",
        "teil",
        "platz",
        "aufgabe",
        "frage",
        "option",
        "step",
        "point",
        "item",
        "chapter",
        "part",
        "question",
    }
)
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
_ORDINAL = re.compile(r"\d{1,4}(?:\.\d{1,2})*")
_QUOTES = "\"'»«“”„‘’([{"
#: Words in front of a number that make it an ordinal: "der 3. Versuch", "am 9. Mai".
_ORDINAL_CUES = frozenset(
    "der die das den dem des am im zum zur vom beim ein eine einen einem jeden jedem diesen".split()
)
#: A mark (or run of marks) followed by closing quotes and brackets, then white space or the end -
#: or a line break.
_END = re.compile(r"([.!?…]+)([\"'»«”“’)\]]*)(?=\s|$)|\n+")
_MARKS_THAT_ALWAYS_END = re.compile(r"[!?…]|\.{3}")
_TRAILING_PUNCT = re.compile(r"[.!?…]+$")
_COMPLETE_WORD = re.compile(r"\S+\s")


def _period_ends_sentence(prefix: str, following: str, *, final: bool) -> bool | None:
    """Does the full stop after `prefix` end a sentence? None: it depends on the word after it,
    which has not arrived yet (or not completely: "M" may become "Mai")."""
    tokens = prefix.split()
    word = tokens[-1].strip(_QUOTES) if tokens else ""
    if word.lower() in _ABBREVIATIONS or (len(word) == 1 and word.isalpha()):
        return False
    if not _ORDINAL.fullmatch(word):
        return True
    if len(tokens) == 1:
        return False  # "1." opening a list item
    before = tokens[-2].strip(_QUOTES + ":").lower()
    if before in _LIST_WORDS or before in _ORDINAL_CUES:
        return False  # "Schritt 1.", "der 3. Versuch", "am 9."
    if not following or not (final or _COMPLETE_WORD.match(following)):
        return None
    nxt = following.split(None, 1)[0].strip(_QUOTES + ",;:")
    if nxt.lower() in _MONTHS or nxt[:1].islower() or nxt[:1].isdigit():
        return False  # "am 22.11. um fünf", "2. oder 3."
    return True


def split_stream(buffer: str, *, final: bool) -> tuple[list[str], str]:
    """The sentences in `buffer` that are complete, and the rest (not yet a sentence).

    With `final` the rest is a sentence too: the stream has ended. Feeding a text in pieces and
    keeping the rest gives the same sentences as splitting the whole text at once, because a
    sentence is only let go when what follows its end mark is known: a mark at the very end of the
    buffer may still grow ("3." into "3.5", "Ja." into "Ja...", a closing quote after it), and a
    number waits for the word after it.
    """
    sentences: list[str] = []
    start = 0
    for match in _END.finditer(buffer):
        if match.start() < start:
            continue
        if match.group(0).startswith("\n"):
            piece = buffer[start : match.start()].strip()
            if piece:
                sentences.append(piece)
            start = match.end()
            continue
        marks, end = match.group(1), match.end()
        if end == len(buffer) and not final:
            break  # the mark may not be complete yet
        if _MARKS_THAT_ALWAYS_END.search(marks):
            ends: bool | None = True
        else:
            ends = _period_ends_sentence(
                buffer[start : match.start()], buffer[end:].lstrip(), final=final
            )
        if ends is None:
            if not final:
                break  # wait for the next word
            ends = True
        if ends:
            sentences.append(buffer[start:end].strip())
            start = end
    rest = buffer[start:]
    if final:
        if rest.strip():
            sentences.append(rest.strip())
        return sentences, ""
    return sentences, rest.lstrip()


def split_sentences(text: str, *, min_chars: int = 4) -> list[str]:
    """`text` as sentences (see `split_stream`), with very short fragments merged into their
    predecessor so a lone "Ok." does not become a chunk of its own with an audible gap after it."""
    pieces, _ = split_stream(text, final=True)
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    if len(merged) > 1 and len(_TRAILING_PUNCT.sub("", merged[-1])) < min_chars:
        last = merged.pop()
        merged[-1] = f"{merged[-1]} {last}"
    return merged
