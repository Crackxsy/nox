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


# ---- install(core): the mute gate is wired into the service that really runs ------------------


class _Speaker:
    def __init__(self) -> None:
        self.said: list[Any] = []

    async def say(self, request: Any) -> None:
        self.said.append(request)


async def _callout_through_install(tmp_path: Any, *, muted: bool) -> list[Any]:
    from nox.core.bus import AsyncEventBus
    from nox.core.config import NoxConfig
    from nox.core.events import E, Event
    from nox.data.db import Database
    from nox.rl.install import install
    from tests.unit.fakes import FakeState

    db = Database(tmp_path / "nox.db")
    db.migrate()
    bus = AsyncEventBus()
    speaker = _Speaker()
    core = SimpleNamespace(
        config=NoxConfig(),
        db=db,
        bus=bus,
        state=FakeState(),
        security=SimpleNamespace(engine=SimpleNamespace(set_profile=lambda *_a, **_k: None)),
        speaker=speaker,
        router=None,
        speech_policy=_policy(muted=muted),
        health=SimpleNamespace(add_check=lambda _check: None),
    )
    runtime = install(core)
    try:
        await bus.publish(
            Event(
                name=E.RL_EVENT,
                payload={"match_id": 1, "kind": "demo", "source": "hud", "confidence": 0.95},
            )
        )
    finally:
        await runtime.stop()
        db.close()
    return speaker.said


async def test_installed_callouts_speak_when_nox_is_not_muted(tmp_path: Any) -> None:
    said = await _callout_through_install(tmp_path, muted=False)
    assert len(said) == 1
    assert said[0].prepared_clip == "careful_demo"
    assert said[0].text == "Vorsicht, Demo."


async def test_installed_callouts_stay_silent_while_nox_is_muted(tmp_path: Any) -> None:
    """The gate has to be wired in `install`, not only exist in the service."""
    assert await _callout_through_install(tmp_path, muted=True) == []
