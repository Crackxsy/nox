"""The microphone notice is spoken - in a real core, where it has to be wired up.

The worker reports `voice.capture_notice` and does not speak it; the core's `CaptureNoticeSpeaker`
does, when the speech policy allows. A unit test of the speaker with a speaker of its own cannot
notice the subscription missing from the application: Nox would then stay deaf without a word.
This boots a core, publishes the event the way the hub does, and looks at what the speaker is
asked to say.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.events import E, Event
from nox.core.state import PrivacyMode
from nox.voice.base import TtsRequest
from tests.integration.test_capabilities import _config


class RecordingSpeaker:
    def __init__(self) -> None:
        self.said: list[TtsRequest] = []

    async def say(self, request: TtsRequest) -> None:
        self.said.append(request)


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    user_config = tmp_path / "user.yaml"
    user_config.write_text(yaml.safe_dump({}), encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(user_config))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    instance = NoxCore(_config(tmp_path), voice=False)
    await asyncio.wait_for(instance.start(), timeout=120)
    try:
        yield instance
    finally:
        await asyncio.wait_for(instance.stop(), timeout=30)


async def notice(core: NoxCore, kind: str) -> list[TtsRequest]:
    """Publish a capture notice and return what the core's speaker was asked to say."""
    speaker = RecordingSpeaker()
    core.speaker.say = speaker.say  # type: ignore[method-assign]
    await core.bus.publish(
        Event(name=E.VOICE_CAPTURE_NOTICE, payload={"notice": kind, "reason": "no input device"})
    )
    return speaker.said


async def test_a_deaf_microphone_is_announced_in_the_users_language(core: NoxCore) -> None:
    said = await notice(core, "deaf")

    assert [r.text for r in said] == ["Ich höre dich gerade nicht."]
    assert said[0].utterance_id.startswith("capture-notice:")


async def test_the_recovery_is_announced_too(core: NoxCore) -> None:
    said = await notice(core, "recovered")

    assert [r.text for r in said] == ["Ich höre dich wieder."]


async def test_nothing_is_said_while_muted(core: NoxCore) -> None:
    await core.state.update("assistant.muted", True, reason="test")

    assert await notice(core, "deaf") == []


async def test_nothing_is_said_in_a_private_privacy_mode(core: NoxCore) -> None:
    await core.security.privacy.set_mode(PrivacyMode.PRIVATE, by="test")

    assert await notice(core, "deaf") == []
