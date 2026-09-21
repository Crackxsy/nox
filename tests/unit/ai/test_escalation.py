"""When a chat turn takes the reasoning chain instead of the chat chain."""

from __future__ import annotations

import pytest

from nox.ai.base import AiRole
from nox.ai.escalation import LONG_QUESTION_WORDS, EscalationPolicy

SHORT_QUESTION = "Was ist ein Vektorindex?"
LONG_QUESTION = (
    "Meine nächtliche Sicherung bricht seit dem letzten Update jedes Mal nach ungefähr zwanzig "
    "Minuten ohne Fehlermeldung ab, obwohl genügend Speicherplatz frei ist; was sind die drei "
    "plausibelsten Ursachen und wie prüfe ich sie der Reihe nach?"
)


def test_a_long_question_without_grounding_takes_the_reasoning_chain() -> None:
    assert len(LONG_QUESTION.split()) >= LONG_QUESTION_WORDS
    decision = EscalationPolicy().decide(LONG_QUESTION, relevance=0.0)
    assert decision.role is AiRole.REASON
    assert "long_question_without_grounding" in decision.reason


def test_a_long_question_with_relevant_memory_stays_on_the_chat_chain() -> None:
    # Grounded is the case the small local model handles well; escalating it would only cost time.
    decision = EscalationPolicy().decide(LONG_QUESTION, relevance=0.81)
    assert decision.role is AiRole.CHAT
    assert decision.reason == ""


def test_a_short_question_is_never_escalated_by_length() -> None:
    decision = EscalationPolicy().decide(SHORT_QUESTION, relevance=0.0)
    assert decision.role is AiRole.CHAT


@pytest.mark.parametrize(
    "text",
    [
        "Denk mal nach: warum hängt der Job?",
        "Denk bitte gründlich nach und sag mir dann, was du meinst.",
        "Überleg dir das genau, bevor du antwortest.",
        "Think hard about this one.",
        "Take your time with this.",
    ],
)
def test_an_explicit_marker_escalates_whatever_the_length(text: str) -> None:
    decision = EscalationPolicy().decide(text, relevance=0.99)
    assert decision.role is AiRole.REASON
    assert decision.reason == "explicit_marker"


@pytest.mark.parametrize(
    "text",
    [
        "Was denkst du darüber?",
        "Ich denke, das liegt am Cache.",
        "Wo ist das Nachschlagewerk?",
    ],
)
def test_ordinary_thinking_words_are_not_markers(text: str) -> None:
    assert EscalationPolicy().decide(text, relevance=0.0).role is AiRole.CHAT


def test_a_disabled_policy_never_escalates() -> None:
    policy = EscalationPolicy(enabled=False)
    assert policy.decide(LONG_QUESTION, relevance=0.0).role is AiRole.CHAT
    assert policy.decide("Denk mal nach.", relevance=0.0).role is AiRole.CHAT
