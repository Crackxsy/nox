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


def test_a_request_to_nox_is_addressed() -> None:
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", "!nox what is your favourite game")
    assert addressed is True
    assert score > 0.5


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


def test_cooldown_does_not_suppress_a_request_to_nox() -> None:
    now = [0.0]
    clf = RelevanceClassifier(["nox"], cooldown_s=10.0, clock=lambda: now[0])
    clf.classify("v1", "hello nox")
    now[0] = 1.0
    score, addressed = clf.classify("v1", "!nox are you there")
    assert addressed is True
    assert score == 1.0


def test_cooldown_expires() -> None:
    now = [0.0]
    clf = RelevanceClassifier(["nox"], cooldown_s=5.0, clock=lambda: now[0])
    first, _ = clf.classify("v1", "hello nox")
    now[0] = 10.0  # past the cooldown window
    second, _ = clf.classify("v1", "hello nox")
    assert second == first


# -- who is being talked to --------------------------------------------------------------------
#
# One row per kind of line: (what the viewer typed, is it a request to Nox). A command belongs to
# whoever owns it - Nox's own commands are answered by the plugin, another bot's by that bot - so
# only `!nox ...` reaches the language model.

ADDRESSING_RULES = [
    # Nox is addressed
    ("hey nox, wie gehts?", True),
    ("Nox?", True),
    ("@nox hi", True),
    ("danke nox!", True),
    ("!nox was ist dein lieblingsspiel", True),
    ("!NOX hallo", True),
    ("  !nox  hallo", True),
    ("!nox, hallo", True),
    ("!nox?", True),
    # another bot's command, and Nox's own commands (answered by the plugin, once)
    ("!lurk", False),
    ("!discord", False),
    ("!lurk nox", False),
    ("!lurk why?", False),
    ("!lurk hey nox, wie gehts?", False),
    ("!rps rock", False),
    ("!funken", False),
    ("!befehle", False),
    ("!streamplan", False),
    ("!commands", False),
    ("!noxious", False),
    ("!", False),
    ("! nox", False),
    # ordinary chatter, including words that merely contain the name
    ("lol", False),
    ("just watching the gameplay", False),
    ("hello everyone", False),
    ("why is the sky blue?", False),
    ("equinox is tonight", False),
    ("noxus top lane again", False),
    ("knoxville", False),
]


@pytest.mark.parametrize(("text", "is_request"), ADDRESSING_RULES)
def test_only_a_request_to_nox_is_addressed(text: str, is_request: bool) -> None:
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", text)
    assert addressed is is_request
    assert (score >= 0.5) is is_request


@pytest.mark.parametrize("text", ["!lurk", "!rps rock", "!funken", "!discord why?", "!lurk nox?"])
def test_a_command_that_is_not_a_request_to_nox_scores_zero(text: str) -> None:
    assert RelevanceClassifier(["nox"]).classify("v1", text) == (0.0, False)


def test_a_command_does_not_start_the_viewers_cooldown() -> None:
    """A viewer who typed `!rps` a moment ago is not "just scored": the next real question still
    counts in full."""
    now = [0.0]
    clf = RelevanceClassifier(["nox"], cooldown_s=10.0, clock=lambda: now[0])
    fresh, _ = RelevanceClassifier(["nox"]).classify("v2", "hey nox?")
    clf.classify("v1", "!rps rock")
    now[0] = 1.0
    assert clf.classify("v1", "hey nox?")[0] == fresh


def test_every_configured_alias_counts_as_the_name() -> None:
    clf = RelevanceClassifier(["nox", "Noxi"])
    assert clf.classify("v1", "hallo noxi")[1] is True
    assert clf.classify("v2", "!noxi hallo")[1] is True
    assert clf.classify("v3", "!lurk noxi")[1] is False


def test_the_bots_own_login_counts_as_a_name() -> None:
    clf = RelevanceClassifier(["nox"])
    assert clf.classify("v1", "@noxbot hi", bot_login="NoxBot") == (0.6, True)
    assert clf.classify("v2", "!noxbot hi", bot_login="noxbot")[1] is True
    assert clf.classify("v3", "@noxbot hi")[1] is False
    assert clf.classify("v4", "!lurk @noxbot", bot_login="noxbot")[1] is False


def test_a_reply_to_the_bots_message_is_addressed_without_its_name() -> None:
    clf = RelevanceClassifier(["nox"])
    score, addressed = clf.classify("v1", "genau das meine ich", replied_to_bot=True)
    assert addressed is True
    assert score >= 0.5
    assert clf.classify("v2", "genau das meine ich") == (0.0, False)


def test_a_reply_that_carries_another_bots_command_is_still_not_addressed() -> None:
    assert RelevanceClassifier(["nox"]).classify("v1", "!lurk", replied_to_bot=True) == (
        0.0,
        False,
    )
