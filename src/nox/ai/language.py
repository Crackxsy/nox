"""Language detection and date/time wording shared by the answer paths that do not use a model.

The deterministic fast path (`nox.ai.fastpath`) and the offline rules provider
(`nox.ai.providers.rules`) both have to say what time it is in the user's language. The wording
lives here so the two cannot drift apart; everything else about those two paths differs, because
one is a normal answer and the other is an honest "no model is reachable" answer.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

Language = Literal["de", "en"]

#: Stop words that only appear in German text. Deliberately small: the explicit language hint from
#: the transcript or the dashboard wins, and this only decides when there is none.
_GERMAN_HINTS = re.compile(
    r"\b(ich|du|bist|ist|nicht|und|wie|was|der|die|das|ein|eine|hallo|moin|servus|uhr|bitte|"
    r"danke|geht|dir|mir|heute|jetzt)\b",
    re.IGNORECASE,
)

_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS_DE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def detect_language(text: str, hint: str | None) -> Language:
    """``de`` or ``en``: the explicit hint wins, then a stop-word heuristic, default ``de``."""
    if hint == "de":
        return "de"
    if hint == "en":
        return "en"
    return "de" if _GERMAN_HINTS.search(text) else "en"


def render_date(now: datetime, language: Language) -> str:
    """The date as a person says it: ``Montag, 21. September 2026`` or ``Monday, September 21,
    2026``."""
    if language == "de":
        return f"{_WEEKDAYS_DE[now.weekday()]}, {now.day}. {_MONTHS_DE[now.month - 1]} {now.year}"
    return now.strftime("%A, %B %d, %Y")


def render_clock(now: datetime, _language: Language) -> str:
    """The wall-clock time as ``14:05``.

    24-hour in both languages: the user interface is German, a spoken answer should not switch
    clock conventions mid-conversation, and the English wording around it ("It's 14:05") reads
    fine to anyone who asked a German assistant a question in English.
    """
    return now.strftime("%H:%M")
