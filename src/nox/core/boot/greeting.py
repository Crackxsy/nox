"""What Nox says when it is ready - including when it is not at its best.

The greeting used to be "Hallo, ich bin Nox. Ich bin bereit." whatever the state. On the product
owner's first real evening Claude Code's login had expired, every answer came from the small local
model, and nothing said so: the answers were simply worse and Nox seemed broken. When the preferred
AI backend is not available at start, the greeting now says which brain is answering instead, why,
and - where there is one - what fixes it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from nox.core.events import HealthStatus

__all__ = ["GREETING", "greeting_text"]

GREETING = {"de": "Hallo, ich bin Nox. Ich bin bereit.", "en": "Hi, I am Nox. I am ready."}

_NAMES = {
    "claude_code": {"de": "Claude", "en": "Claude"},
    "ollama": {"de": "das lokale Modell", "en": "the local model"},
    "rules": {"de": "feste Regeln", "en": "fixed rules"},
}

#: Known failure reasons -> what the user can do about them. Matched case-insensitively against the
#: health reason; the first match wins. A reason not listed here is not paraphrased into advice.
_FIXES: list[tuple[tuple[str, ...], dict[str, str]]] = [
    (
        ("oauth", "authenticate", "not logged in", "login"),
        {
            "de": "Die Anmeldung ist abgelaufen. Starte einmal claude in einer Konsole und gib "
            "slash login ein.",
            "en": "The login has expired. Run claude in a terminal once and type slash login.",
        },
    ),
    (
        ("not found", "not installed", "no such file"),
        {
            "de": "Claude Code ist auf diesem Rechner nicht installiert.",
            "en": "Claude Code is not installed on this computer.",
        },
    ),
    (
        ("timeout", "timed out", "unreachable", "connection"),
        {
            "de": "Es kommt gerade keine Verbindung zustande.",
            "en": "It cannot be reached right now.",
        },
    ),
]


class _Health(Protocol):
    @property
    def status(self) -> HealthStatus: ...
    @property
    def reason(self) -> str: ...


def _pick(table: Mapping[str, str], language: str) -> str:
    return table["de"] if language.startswith("de") else table["en"]


def _name(provider: str, language: str) -> str:
    return _pick(_NAMES.get(provider, {"de": provider, "en": provider}), language)


def _fix_for(reason: str, language: str) -> str:
    lowered = reason.lower()
    for needles, fix in _FIXES:
        if any(needle in lowered for needle in needles):
            return _pick(fix, language)
    return ""


def greeting_text(language: str, health: Mapping[str, _Health], chain: Sequence[str]) -> str:
    """The spoken greeting for the current health of the backends in `chain`, best first.

    The first backend that is available answers; if that is not the first in the chain, the
    greeting says so. A backend whose health is not known yet is not reported as down.
    """
    base = _pick(GREETING, language)
    if not chain:
        return base
    preferred = chain[0]
    state = health.get(f"ai.{preferred}")
    if state is None or state.status is HealthStatus.AVAILABLE:
        return base
    answering = next(
        (
            name
            for name in chain[1:]
            if (h := health.get(f"ai.{name}")) is not None and h.status is HealthStatus.AVAILABLE
        ),
        None,
    )
    german = language.startswith("de")
    if german:
        head = f"Hallo, ich bin Nox. {_name(preferred, language)} ist gerade nicht erreichbar"
        tail = (
            f", ich antworte erstmal über {_name(answering, language)} - das kann einfacher "
            "klingen."
            if answering is not None
            else "."
        )
    else:
        head = f"Hi, I am Nox. {_name(preferred, language)} is not available right now"
        tail = (
            f", so {_name(answering, language)} is answering for now - it may sound simpler."
            if answering is not None
            else "."
        )
    fix = _fix_for(state.reason, language)
    return f"{head}{tail} {fix}" if fix else f"{head}{tail}"
