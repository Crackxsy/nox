"""Which language a Rocket League callout is spoken in follows the speech language setting."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nox.rl.install import _speech_language


def _core(speech_language: str) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(identity=SimpleNamespace(speech_language=speech_language))
    )


@pytest.mark.parametrize(
    ("configured", "spoken"), [("de", "de"), ("en", "en"), ("auto", "de"), ("", "de")]
)
def test_callouts_are_spoken_in_a_concrete_language(configured: str, spoken: str) -> None:
    assert _speech_language(_core(configured)) == spoken


def _policy(*, muted: bool) -> Any:
    from nox.core.config import NoxConfig
    from nox.core.speech_policy import SpeechPolicy
    from tests.unit.fakes import FakeState

    state = FakeState()
    state.state.assistant.muted = muted
    return SpeechPolicy(
        state=state, config=NoxConfig(), active_zone=lambda: None, privacy_mode=lambda: "balanced"
    )


@pytest.mark.parametrize(("muted", "allowed"), [(True, False), (False, True)])
def test_callouts_follow_the_real_speech_policy_on_mute(muted: bool, allowed: bool) -> None:
    from nox.rl.install import _may_speak

    core = SimpleNamespace(speech_policy=_policy(muted=muted), state=None)
    assert _may_speak(core) is allowed


@pytest.mark.parametrize(("muted", "allowed"), [(True, False), (False, True)])
def test_callouts_follow_the_mute_flag_while_no_policy_is_built(muted: bool, allowed: bool) -> None:
    from nox.rl.install import _may_speak
    from tests.unit.fakes import FakeState

    state = FakeState()
    state.state.assistant.muted = muted
    assert _may_speak(SimpleNamespace(speech_policy=None, state=state)) is allowed
