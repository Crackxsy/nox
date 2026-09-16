"""Sentence splitter for streaming TTS."""

from __future__ import annotations

from nox.voice.tts.sentences import split_sentences


def test_basic_split() -> None:
    assert split_sentences("Hallo! Ich bin Nox. Wie geht es dir?") == [
        "Hallo!",
        "Ich bin Nox.",
        "Wie geht es dir?",
    ]


def test_newlines_and_whitespace() -> None:
    assert split_sentences("Erster Satz\n\nZweiter Satz  ") == ["Erster Satz", "Zweiter Satz"]


def test_decimals_and_abbreviations_stay_together() -> None:
    assert split_sentences("Das kostet 3.5 Euro. Also z.B. morgen. Dr. Müller kommt.") == [
        "Das kostet 3.5 Euro.",
        "Also z.B. morgen.",
        "Dr. Müller kommt.",
    ]


def test_short_fragments_merge() -> None:
    assert split_sentences("Ok. Dann machen wir das so. Ja.") == ["Ok. Dann machen wir das so. Ja."]


def test_empty() -> None:
    assert split_sentences("") == []
    assert split_sentences("   \n ") == []
