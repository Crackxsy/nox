"""Wake word and kill phrase matching."""

from __future__ import annotations

import pytest

from nox.voice.stt.wake_word import WakeWordMatcher, normalize


@pytest.fixture
def matcher() -> WakeWordMatcher:
    return WakeWordMatcher("Nox")


@pytest.mark.parametrize(
    "text",
    [
        "Nox, wie spät ist es?",
        "nox wie spät ist es",
        "Knox, wie spät ist es?",
        "Nocks wie spät ist es",
        "Hey Nox, wie spät ist es?",
        "Hallo Nox! Wie spät ist es?",
        "Nocs, what time is it?",
    ],
)
def test_addressed_variants(matcher: WakeWordMatcher, text: str) -> None:
    m = matcher.match(text)
    assert m.addressed
    assert not m.kill
    assert m.remainder.lower().startswith(("wie", "what"))


@pytest.mark.parametrize(
    "text",
    [
        "Wie spät ist es?",
        "Ich rede gerade mit dem Chat über Nox.",  # name mid-sentence: not addressed
        "Nochmal bitte",  # 'nochmal' must not fuzzy-match
        "Nein, das war nix",
        "",
    ],
)
def test_not_addressed(matcher: WakeWordMatcher, text: str) -> None:
    m = matcher.match(text)
    assert not m.addressed
    assert not m.kill


@pytest.mark.parametrize(
    "text",
    [
        "Nox Notaus",
        "Nox, Not-Aus!",
        "nox not aus",
        "Knox Notaus.",
        "Nox emergency stop",
        "Nox, emergency stop now",
        "Alles klar. Nox Notaus.",  # kill phrase later in the utterance still counts
    ],
)
def test_kill_phrase(matcher: WakeWordMatcher, text: str) -> None:
    assert matcher.match(text).kill


@pytest.mark.parametrize(
    "text", ["Notaus", "emergency stop", "Nox, bitte nicht ausmachen", "Nox stop"]
)
def test_no_kill_without_full_phrase(matcher: WakeWordMatcher, text: str) -> None:
    assert not matcher.match(text).kill


def test_normalize() -> None:
    assert normalize("  Hey,  NOX!! Wie   geht's? ") == "hey nox wie geht s"


@pytest.mark.parametrize(
    ("text", "request_text"),
    [
        (
            "Nox, merk dir: Treffen mit Anna am 3. Mai um 18:30 Uhr.",
            "merk dir: Treffen mit Anna am 3. Mai um 18:30 Uhr.",
        ),
        ("Nox, wie spät ist es?", "wie spät ist es?"),
        ("Hey Nox, Wie viel sind 3,5 mal 2?", "Wie viel sind 3,5 mal 2?"),
        ("Hallo Nox! Schalte das Licht in der Küche ein.", "Schalte das Licht in der Küche ein."),
        ("nox   stell einen Timer auf 10 Minuten", "stell einen Timer auf 10 Minuten"),
        ("Nox , Rocket League starten", "Rocket League starten"),
        ("Nox - Musik leiser", "Musik leiser"),
        ("Knox: Was kostet das Ticket nach Köln?", "Was kostet das Ticket nach Köln?"),
        ("Nox", ""),
        ("Nox,", ""),
    ],
)
def test_the_request_after_the_wake_word_keeps_the_words_as_transcribed(
    matcher: WakeWordMatcher, text: str, request_text: str
) -> None:
    m = matcher.match(text)
    assert m.addressed
    assert m.remainder == request_text


def test_text_that_is_not_addressed_is_returned_as_transcribed(matcher: WakeWordMatcher) -> None:
    assert matcher.match("  Wie spät ist es, 3,5 Uhr?  ").remainder == "Wie spät ist es, 3,5 Uhr?"
    assert matcher.match("").remainder == ""


def test_a_greeting_and_the_name_are_both_removed_from_the_request(
    matcher: WakeWordMatcher,
) -> None:
    assert matcher.match("Okay Nox, Anna kommt um 18:30.").remainder == "Anna kommt um 18:30."


def test_a_kill_phrase_after_the_name_is_still_found_when_the_request_is_cut(
    matcher: WakeWordMatcher,
) -> None:
    m = matcher.match("Nox, Not-Aus!")
    assert m.kill and m.addressed
