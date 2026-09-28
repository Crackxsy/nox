"""Wake word and kill phrase matching."""

from __future__ import annotations

import pytest

from nox.voice.stt.wake_word import WakeWordMatcher, mentions_kill_phrase, normalize


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


#: (utterance, is the kill phrase). The phrase must *be* the utterance - modulo greetings, filler
#: words and punctuation - and start with the wake word; quoting or discussing it never counts.
KILL_CASES: list[tuple[str, bool]] = [
    # the phrase itself, in the spellings Whisper produces
    ("Nox Notaus", True),
    ("Nox, Not-Aus!", True),
    ("nox not aus", True),
    ("Knox Notaus.", True),
    ("Nox Nottaus", True),
    ("Nox Notauss", True),
    ("Nox, Nota aus", True),
    ("Nuks Notaus", True),
    ("Nocks, Notaus!", True),
    ("NoxNotaus", True),
    ("Nox Nothalt", True),
    ("Nox, Not-Halt", True),
    # filler words and greetings around it
    ("Nox bitte Notaus", True),
    ("Nox... ähm Notaus", True),
    ("Hey Nox, Notaus!", True),
    ("Äh, Nox, Notaus, sofort!", True),
    ("Nox Notaus jetzt", True),
    ("Nox, Notaus, Notaus!", True),
    ("Nox Nox Notaus", True),
    # English phrase, and German "Notaus" transcribed as English
    ("Nox emergency stop", True),
    ("Nox, emergency stop now", True),
    ("Nox, emergency shutdown please", True),
    ("Nox, not house.", True),
    ("Knox not out", True),
    ("Knox, note out!", True),
    # quoting or talking about the phrase
    ("Sag einfach Nox Notaus", False),
    ("Was macht der Notaus-Knopf?", False),
    ("Nox, Notaus-Knopf erklären", False),
    ("Nox, was bedeutet Notaus?", False),
    ("Alles klar. Nox Notaus.", False),
    ("Wenn du Nox Notaus sagst, stoppt alles", False),
    ("Nox Notaus ist ein gutes Wort", False),
    ("Man sagt Nox, Notaus.", False),
    # incomplete or different phrases
    ("Notaus", False),
    ("emergency stop", False),
    ("Nox, bitte nicht ausmachen", False),
    ("Nox stop", False),
    ("Nox, not now", False),
    ("Nox Notar", False),
    ("Nox, Notruf", False),
    ("Nox not out of the woods yet", False),
    ("", False),
]


@pytest.mark.parametrize(("text", "kill"), KILL_CASES)
def test_kill_phrase_table(matcher: WakeWordMatcher, text: str, kill: bool) -> None:
    assert matcher.match(text).kill is kill


def test_the_kill_table_covers_both_sides() -> None:
    assert sum(1 for _, kill in KILL_CASES if kill) >= 15
    assert sum(1 for _, kill in KILL_CASES if not kill) >= 15


@pytest.mark.parametrize(
    ("text", "mentions"),
    [
        ("Sag einfach Nox Notaus, dann stoppe ich.", True),
        ("Der Not-Aus stoppt alles.", True),
        ("Say Nox emergency stop to stop me.", True),
        ("Ich bin Nox und helfe dir gern.", False),
        ("Es ist nicht aus.", False),
    ],
)
def test_mentions_kill_phrase_is_the_loose_check(text: str, mentions: bool) -> None:
    assert mentions_kill_phrase(text) is mentions


def test_normalize() -> None:
    assert normalize("  Hey,  NOX!! Wie   geht's? ") == "hey nox wie geht s"
