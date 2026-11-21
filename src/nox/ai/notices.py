"""What Nox says when no model answers: plain sentences with the reason and the next step.

A failed turn used to end in a log line, or in "That did not work". These texts are the other side
of `NoProviderCause`: a plan limit that is used up, a privacy mode that forbids the cloud, a model
that does not answer, nothing set up at all. Every sentence says what happened and what to do, in
German and English. This module only words things; whether and where a sentence is spoken is the
proactive layer's call (`nox.proactive.ai_notices`), and the limit itself is the router's
(`nox.ai.router`).
"""

from __future__ import annotations

from datetime import datetime

from nox.ai.errors import NoProviderCause
from nox.ai.language import Language, render_clock, render_weekday

__all__ = [
    "GAVE_UP_ON_TOOLS",
    "failure_notice",
    "limit_notice",
    "reset_phrase",
]

#: Said when the model kept asking for tools until the turn ran out of rounds. It names what
#: happened and what helps; the detail of which tool failed belongs in the log.
GAVE_UP_ON_TOOLS = {
    "de": (
        "Das hat nicht geklappt: Ich habe mehrere Werkzeuge versucht und bin damit nicht ans Ziel "
        "gekommen. Sag mir genauer, was ich tun soll, oder versuch es gleich noch einmal."
    ),
    "en": (
        "That did not work: I tried several tools and did not get anywhere. Tell me more "
        "precisely what to do, or try again in a moment."
    ),
}

#: What answers while Claude is out, by `AiProviderLimited.fallback`.
_MEANWHILE = {
    "de": {
        "local": "Bis dahin nutze ich das lokale Modell.",
        "rules": "Bis dahin antworte ich nur mit einfachen, festen Antworten.",
        "none": "Bis dahin kann ich dazu nichts beantworten.",
    },
    "en": {
        "local": "Until then I use the local model.",
        "rules": "Until then I only give simple, fixed answers.",
        "none": "Until then I cannot answer that.",
    },
}

_FAILURE = {
    "de": {
        NoProviderCause.PRIVACY: (
            "Im aktuellen Privatsphäre-Modus darf ich keine Cloud-Modelle nutzen und ein lokales "
            "Modell antwortet nicht. Starte Ollama oder wechsle den Modus."
        ),
        NoProviderCause.BUDGET: (
            "Das Tagesbudget für Hintergrundaufgaben ist aufgebraucht. Sie laufen morgen wieder."
        ),
        NoProviderCause.TIMEOUT: (
            "Das Modell antwortet gerade nicht, es hat zu lange gedauert. Versuch es gleich noch "
            "einmal."
        ),
        NoProviderCause.FAILED: (
            "Das Modell antwortet gerade nicht. Versuch es gleich noch einmal; wenn es so bleibt, "
            "prüf die Claude-Anmeldung und ob Ollama läuft."
        ),
        NoProviderCause.UNAVAILABLE: (
            "Kein Gehirn verfügbar: Claude ist nicht angemeldet oder nicht installiert und es "
            "läuft kein lokales Modell. Melde Claude an oder starte Ollama."
        ),
        NoProviderCause.INTERNAL: (
            "Etwas ist schiefgegangen, nicht beim Modell, sondern bei mir. Versuch es gleich noch "
            "einmal; wenn es so bleibt, schau ins Protokoll."
        ),
    },
    "en": {
        NoProviderCause.PRIVACY: (
            "The current privacy mode does not allow cloud models and no local model is "
            "answering. Start Ollama or change the mode."
        ),
        NoProviderCause.BUDGET: (
            "Today's budget for background tasks is used up. They run again tomorrow."
        ),
        NoProviderCause.TIMEOUT: (
            "The model is not answering right now, it took too long. Try again in a moment."
        ),
        NoProviderCause.FAILED: (
            "The model is not answering right now. Try again in a moment; if it stays that way, "
            "check the Claude login and whether Ollama is running."
        ),
        NoProviderCause.UNAVAILABLE: (
            "No brain available: Claude is not signed in or not installed and no local model is "
            "running. Sign in to Claude or start Ollama."
        ),
        NoProviderCause.INTERNAL: (
            "Something went wrong - not with the model, with me. Try again in a moment; if it "
            "stays that way, look at the log."
        ),
    },
}


def _language(language: str) -> Language:
    return "en" if language[:2] == "en" else "de"


def reset_phrase(until: datetime, now: datetime, language: str) -> str:
    """When a limit ends, as a person says it: "14:30 Uhr", "morgen 14:30 Uhr", "Montag 14:30 Uhr".

    Both times are read in the machine's zone, so "today" is the user's today.
    """
    lang = _language(language)
    when, today = until.astimezone(), now.astimezone()
    clock = render_clock(when, lang)
    days = (when.date() - today.date()).days
    if lang == "de":
        if days <= 0:
            return f"{clock} Uhr"
        if days == 1:
            return f"morgen {clock} Uhr"
        return f"{render_weekday(when, lang)} {clock} Uhr"
    if days <= 0:
        return clock
    return f"tomorrow {clock}" if days == 1 else f"{render_weekday(when, lang)} {clock}"


def _minutes_until(until: datetime, now: datetime) -> int:
    return max(1, round((until - now).total_seconds() / 60))


def limit_notice(
    language: str, *, until: datetime, now: datetime, estimated: bool, fallback: str
) -> str:
    """The one-time notice that Claude's usage limit is used up, and what answers meanwhile.

    `estimated`: the limit did not say when it ends, so the notice says when Nox tries again.
    """
    lang = _language(language)
    meanwhile = _MEANWHILE[lang].get(fallback, _MEANWHILE[lang]["rules"])
    if estimated:
        minutes = _minutes_until(until, now)
        if lang == "de":
            return (
                f"Mein Claude-Kontingent ist gerade aufgebraucht, ich versuche es in etwa "
                f"{minutes} Minuten wieder. {meanwhile}"
            )
        return (
            f"My Claude usage limit is used up right now; I will try again in about {minutes} "
            f"minutes. {meanwhile}"
        )
    when = reset_phrase(until, now, lang)
    if lang == "de":
        return f"Mein Claude-Kontingent ist bis {when} aufgebraucht. {meanwhile}"
    return f"My Claude usage limit is used up until {when}. {meanwhile}"


def failure_notice(
    language: str,
    cause: NoProviderCause,
    *,
    until: datetime | None = None,
    now: datetime,
    estimated: bool = False,
) -> str:
    """The reply to a turn that no model answered: what happened and what to do next."""
    lang = _language(language)
    if cause is not NoProviderCause.LIMIT:
        return _FAILURE[lang].get(cause, _FAILURE[lang][NoProviderCause.FAILED])
    exact = until is not None and not estimated
    minutes = _minutes_until(until, now) if until is not None else 15
    if lang == "de":
        head = (
            f"Mein Claude-Kontingent ist bis {reset_phrase(until, now, lang)} aufgebraucht"
            if until is not None and exact
            else "Mein Claude-Kontingent ist gerade aufgebraucht"
        )
        retry = "danach" if exact else f"in etwa {minutes} Minuten"
        return (
            f"{head} und ein lokales Modell antwortet nicht. Versuch es {retry} noch einmal "
            "oder richte ein lokales Modell ein."
        )
    head = (
        f"My Claude usage limit is used up until {reset_phrase(until, now, lang)}"
        if until is not None and exact
        else "My Claude usage limit is used up right now"
    )
    retry = "afterwards" if exact else f"in about {minutes} minutes"
    return f"{head} and no local model is answering. Try again {retry} or set up a local model."
