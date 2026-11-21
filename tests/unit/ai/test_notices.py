"""The sentences Nox says when no model answers: reason and next step, in both languages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nox.ai.errors import NoProviderCause
from nox.ai.notices import GAVE_UP_ON_TOOLS, failure_notice, limit_notice, reset_phrase

# Noon in UTC; the phrases are rendered in the machine's zone, so the tests compare against what
# `astimezone()` says instead of assuming a zone.
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def local_clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def test_a_reset_later_today_is_just_the_time() -> None:
    until = NOW + timedelta(minutes=30)
    same_day = until.astimezone().date() == NOW.astimezone().date()

    assert reset_phrase(until, NOW, "de") == (
        f"{local_clock(until)} Uhr" if same_day else f"morgen {local_clock(until)} Uhr"
    )


def test_a_reset_the_day_after_says_tomorrow() -> None:
    until = NOW + timedelta(days=1)

    assert reset_phrase(until, NOW, "de") == f"morgen {local_clock(until)} Uhr"
    assert reset_phrase(until, NOW, "en") == f"tomorrow {local_clock(until)}"


def test_a_reset_further_away_names_the_weekday() -> None:
    until = NOW + timedelta(days=4)
    weekday_de = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    local = until.astimezone()

    assert (
        reset_phrase(until, NOW, "de") == f"{weekday_de[local.weekday()]} {local_clock(until)} Uhr"
    )
    assert reset_phrase(until, NOW, "en") == f"{local:%A} {local_clock(until)}"


@pytest.mark.parametrize(
    ("fallback", "german", "english"),
    [
        ("local", "lokale Modell", "local model"),
        ("rules", "einfachen, festen Antworten", "simple, fixed answers"),
        ("none", "nichts beantworten", "cannot answer that"),
    ],
)
def test_the_limit_notice_says_what_answers_meanwhile(
    fallback: str, german: str, english: str
) -> None:
    until = NOW + timedelta(hours=2)

    de = limit_notice("de", until=until, now=NOW, estimated=False, fallback=fallback)
    en = limit_notice("en", until=until, now=NOW, estimated=False, fallback=fallback)

    assert de.startswith("Mein Claude-Kontingent ist bis ") and german in de
    assert en.startswith("My Claude usage limit is used up until ") and english in en
    assert "Uhr" in de


def test_a_limit_without_a_reset_time_promises_a_retry_not_an_end() -> None:
    until = NOW + timedelta(minutes=15)

    de = limit_notice("de", until=until, now=NOW, estimated=True, fallback="local")
    en = limit_notice("en", until=until, now=NOW, estimated=True, fallback="local")

    assert "in etwa 15 Minuten wieder" in de and " bis " not in de.split(".")[0]
    assert "about 15 minutes" in en


def test_an_unknown_fallback_is_worded_as_the_fixed_answers_it_must_be() -> None:
    text = limit_notice(
        "de", until=NOW + timedelta(hours=1), now=NOW, estimated=False, fallback="?"
    )

    assert "festen Antworten" in text


def test_every_cause_of_a_failed_turn_has_a_sentence_in_both_languages() -> None:
    for cause in NoProviderCause:
        for language in ("de", "en"):
            text = failure_notice(language, cause, until=NOW + timedelta(hours=1), now=NOW)
            assert len(text) > 40, (cause, language)


@pytest.mark.parametrize(
    ("cause", "reason", "next_step"),
    [
        (NoProviderCause.PRIVACY, "Privatsphäre-Modus", "Starte Ollama"),
        (NoProviderCause.BUDGET, "Tagesbudget", "morgen wieder"),
        (NoProviderCause.TIMEOUT, "zu lange gedauert", "noch einmal"),
        (NoProviderCause.FAILED, "antwortet gerade nicht", "Claude-Anmeldung"),
        (
            NoProviderCause.UNAVAILABLE,
            "Kein Gehirn verfügbar",
            "Melde Claude an oder starte Ollama",
        ),
    ],
)
def test_a_failure_says_what_happened_and_what_to_do(
    cause: NoProviderCause, reason: str, next_step: str
) -> None:
    text = failure_notice("de", cause, now=NOW)

    assert reason in text and next_step in text


def test_a_limit_failure_names_the_end_when_it_is_known() -> None:
    until = NOW + timedelta(hours=2)

    known = failure_notice("de", NoProviderCause.LIMIT, until=until, now=NOW)
    guessed = failure_notice("de", NoProviderCause.LIMIT, until=until, now=NOW, estimated=True)
    unknown = failure_notice("en", NoProviderCause.LIMIT, now=NOW)

    assert "bis " in known and "Uhr" in known and "danach noch einmal" in known
    assert "in etwa 120 Minuten" in guessed and "Uhr" not in guessed
    assert "right now" in unknown and "set up a local model" in unknown


def test_giving_up_on_tools_says_what_was_tried_and_what_helps() -> None:
    assert "mehrere Werkzeuge" in GAVE_UP_ON_TOOLS["de"] and "genauer" in GAVE_UP_ON_TOOLS["de"]
    assert "several tools" in GAVE_UP_ON_TOOLS["en"] and "try again" in GAVE_UP_ON_TOOLS["en"]
    for text in GAVE_UP_ON_TOOLS.values():
        assert text not in ("Das hat nicht funktioniert.", "That did not work.")
