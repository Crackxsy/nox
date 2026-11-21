"""Which chat lines get a language-model answer: the real `RelevanceClassifier` feeds the real
`StreamResponder`, so this is the rule a viewer experiences. Nox answers when he is spoken to,
never to another bot's command, and never a second time on top of the plugin's own answer to one
of his commands."""

from __future__ import annotations

import pytest
from nox_plugin_twitch.relevance import RelevanceClassifier

from nox.core.config import RelevanceConfig
from nox.stream.responder import StreamResponder
from tests.unit.fakes import FakeBus, MutableClock
from tests.unit.stream.test_responder import (
    FakeCompleteRouter,
    FakeExecutor,
    FakeKillSwitch,
    _message,
)

#: (what the viewer typed, does the model answer)
ANSWER_RULES = [
    ("hey nox, wie gehts?", True),
    ("@nox hi", True),
    ("!nox was ist dein lieblingsspiel", True),
    ("!lurk", False),
    ("!discord", False),
    ("!lurk why?", False),
    ("!lurk hey nox, wie gehts?", False),
    ("!rps rock", False),
    ("!funken", False),
    ("!befehle", False),
    ("!streamplan", False),
    ("equinox is tonight", False),
    ("just watching the gameplay", False),
]


@pytest.mark.parametrize(("text", "answered"), ANSWER_RULES)
async def test_the_model_answers_only_when_nox_is_spoken_to(text: str, answered: bool) -> None:
    bus = FakeBus()
    router = FakeCompleteRouter()
    executor = FakeExecutor()
    StreamResponder(
        bus, router, executor, RelevanceConfig(), FakeKillSwitch(), clock=MutableClock()
    ).start()
    relevance, addressed = RelevanceClassifier(["nox"]).classify("v1", text)
    await _message(bus, text=text, addressed=addressed, relevance=relevance)
    assert len(router.requests) == int(answered)
    assert len(executor.calls) == int(answered)
