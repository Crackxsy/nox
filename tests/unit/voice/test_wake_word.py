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
    assert m.remainder.startswith(("wie", "what"))


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
