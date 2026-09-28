"""`RelevanceClassifier` - bot-name mention, question, command, greeting, per-viewer cooldown."""

from __future__ import annotations

import pytest
from nox_plugin_twitch.relevance import RelevanceClassifier


def test_plain_chatter_scores_low_and_is_not_addressed() -> None:
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", "just watching the gameplay")
    assert score < 0.5
    assert addressed is False


def test_bot_name_mention_is_addressed() -> None:
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", "hey nox, how are you?")
    assert addressed is True
    assert score > 0.5


@pytest.mark.parametrize("text", ["!rps rock", "!help", "!funken", "!discord", "!nox hi?"])
def test_a_command_is_never_addressed_to_the_conversation(text: str) -> None:
    """A built-in (or another bot) answers it; an LLM reply on top would answer it twice."""
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", text)
    assert addressed is False
    assert score == 0.0


def test_question_raises_score() -> None:
    clf = RelevanceClassifier(["nox"])
    score_q, _ = clf.classify("v1", "why is the sky blue?")
    score_plain, _ = clf.classify("v2", "the sky is blue")
    assert score_q > score_plain


def test_greeting_raises_score() -> None:
    clf = RelevanceClassifier(["nox"])
    score_greet, _ = clf.classify("v1", "hello everyone")
    score_plain, _ = clf.classify("v2", "everyone is here")
    assert score_greet > score_plain


def test_per_viewer_cooldown_dampens_repeat_chatter() -> None:
    now = [0.0]
    clf = RelevanceClassifier(["nox"], cooldown_s=10.0, clock=lambda: now[0])
    first, _ = clf.classify("v1", "hello nox")
    now[0] = 1.0  # well within the 10s cooldown
    second, _ = clf.classify("v1", "hello nox")
    assert second < first


def test_cooldown_expires() -> None:
    now = [0.0]
    clf = RelevanceClassifier(["nox"], cooldown_s=5.0, clock=lambda: now[0])
    first, _ = clf.classify("v1", "hello nox")
    now[0] = 10.0  # past the cooldown window
    second, _ = clf.classify("v1", "hello nox")
    assert second == first
