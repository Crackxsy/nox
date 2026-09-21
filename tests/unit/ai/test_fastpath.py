"""The deterministic fast path: what it answers, and - far more important - what it refuses to.

A matcher in front of the language model is only acceptable if it cannot steal a real question.
Most of this file is that guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nox.ai.fastpath import MAX_WORDS, FastPath, normalize

MORNING = datetime(2026, 9, 21, 8, 30, tzinfo=UTC)
AFTERNOON = datetime(2026, 9, 21, 14, 5, tzinfo=UTC)
EVENING = datetime(2026, 9, 21, 20, 45, tzinfo=UTC)


def at(moment: datetime) -> FastPath:
    return FastPath(clock=lambda: moment)


#: Utterances that add nothing for a language model to do. Matching these is the whole point.
PHATIC = [
    ("Hallo", "greeting"),
    ("hallo!", "greeting"),
    ("Hey Nox", "greeting"),
    ("Nox, hallo", "greeting"),
    ("Guten Morgen", "greeting"),
    ("Moin", "greeting"),
    ("Hello", "greeting"),
    ("Good evening", "greeting"),
    ("Danke", "thanks"),
    ("danke dir!", "thanks"),
    ("Vielen Dank", "thanks"),
    ("thank you", "thanks"),
    ("Tschüss", "farewell"),
    ("Bis später", "farewell"),
    ("Gute Nacht", "farewell"),
    ("see you", "farewell"),
    ("Wie spät ist es?", "time"),
    ("wie spät ist es denn?", "time"),
    ("Wie viel Uhr ist es?", "time"),
    ("Uhrzeit", "time"),
    ("what time is it", "time"),
    ("Welcher Tag ist heute?", "date"),
    ("Welches Datum haben wir?", "date"),
    ("what's the date", "date"),
    ("Wie geht es dir?", "status"),
    ("wie gehts?", "status"),
    ("Alles klar?", "status"),
    ("how are you", "status"),
]

#: Real requests that happen to start with, contain, or rhyme with a phatic phrase. Every one of
#: these must reach the model. This list is the regression net for the fast path.
REAL_QUESTIONS = [
    "Hallo, kannst du mir beim Deployment helfen?",
    "Hallo Nox, wie spät fährt der letzte Zug?",
    "Danke, aber wie mache ich das ohne sudo?",
    "Wie spät sollte ich morgen losfahren, wenn die Fahrt zwei Stunden dauert?",
    "Welcher Tag ist für das Release am besten geeignet?",
    "Wie geht es dem Server nach dem Neustart?",
    "Wie geht das mit den Umgebungsvariablen?",
    "Guten Morgen, was steht heute in meinem Kalender?",
    "Erklär mir, was ein Vektorindex ist.",
    "Sag Hallo zu meinem kleinen Freund",
    "Was bedeutet Uhrzeit in der Datenbank?",
    "how are you going to solve this problem",
    "Bis später heißt auf Englisch was?",
    "Schreib mir eine kurze Antwort auf die Mail und bedanke dich darin.",
]


@pytest.mark.parametrize(("text", "intent"), PHATIC)
def test_phatic_utterances_are_answered_without_a_model(text: str, intent: str) -> None:
    answer = at(AFTERNOON).match(text)
    assert answer is not None, f"{text!r} should have been answered deterministically"
    assert answer.intent == intent
    assert answer.text.strip()


@pytest.mark.parametrize("text", REAL_QUESTIONS)
def test_a_real_question_is_never_swallowed(text: str) -> None:
    assert at(AFTERNOON).match(text) is None, f"{text!r} must reach the language model"


def test_anything_longer_than_the_word_cap_is_refused_outright() -> None:
    # Even a sentence built only from words the matcher knows.
    long_greeting = " ".join(["hallo"] * (MAX_WORDS + 1))
    assert at(AFTERNOON).match(long_greeting) is None


def test_empty_and_whitespace_reach_nobody() -> None:
    assert at(AFTERNOON).match("") is None
    assert at(AFTERNOON).match("   ") is None


def test_disabled_fast_path_answers_nothing() -> None:
    assert FastPath(enabled=False, clock=lambda: AFTERNOON).match("Hallo") is None


def test_time_and_date_answers_come_from_the_clock() -> None:
    fast = at(AFTERNOON)
    time_answer = fast.match("Wie spät ist es?")
    date_answer = fast.match("Welcher Tag ist heute?")
    assert time_answer is not None and date_answer is not None
    assert time_answer.text == "Es ist 14:05 Uhr."
    assert date_answer.text == "Heute ist Montag, 21. September 2026."


def test_english_time_and_date_use_english_wording() -> None:
    fast = at(AFTERNOON)
    time_answer = fast.match("what time is it")
    date_answer = fast.match("what's the date")
    assert time_answer is not None and date_answer is not None
    assert time_answer.text == "It's 14:05."
    assert date_answer.text == "Today is Monday, September 21, 2026."


def test_the_greeting_follows_the_time_of_day() -> None:
    assert at(MORNING).match("Hallo").text.startswith("Guten Morgen.")  # type: ignore[union-attr]
    assert at(AFTERNOON).match("Hallo").text.startswith("Hallo.")  # type: ignore[union-attr]
    assert at(EVENING).match("Hallo").text.startswith("Guten Abend.")  # type: ignore[union-attr]


def test_the_language_hint_wins_over_the_heuristic() -> None:
    answer = at(AFTERNOON).match("Hallo", "en")
    assert answer is not None and answer.text.startswith("Hello.")


def test_no_answer_claims_a_mood_or_an_availability_it_cannot_back_up() -> None:
    # Honesty rule: the fast path knows nothing about how the system is doing, so it must not say.
    status = at(AFTERNOON).match("Wie geht es dir?")
    assert status is not None
    lowered = status.text.lower()
    assert "mir geht" not in lowered  # no invented feelings
    assert "online" not in lowered and "verfügbar" not in lowered  # no invented availability


def test_normalize_strips_address_punctuation_and_case() -> None:
    assert normalize("  Nox, hallo!!  ") == "hallo"
    assert normalize("Hey Nox") == "hey"
    assert normalize("what's the date?") == "whats the date"


def test_a_bare_wake_word_is_not_turned_into_a_greeting() -> None:
    # "Nox" on its own is an address, not a question and not a greeting; the model decides.
    assert normalize("Nox") == "nox"
    assert at(AFTERNOON).match("Nox") is None
