"""The failure notice is spoken only where speaking is allowed - in a real core.

`Orchestrator.failure_may_speak` stays quiet until `NoxCore` wires it to the speech policy, and a
unit test with its own gate cannot notice the wiring missing: Nox would then say "no model
answered" during mute and in a game. This boots a core and asks the orchestrator itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.app import DEFAULTS_PATH, NoxCore
from nox.core.state import Mode, PrivacyMode
from nox.voice.base import TtsRequest
from tests.integration.test_capabilities import _config


class RecordingSpeaker:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, request: TtsRequest) -> None:
        self.said.append(request.text)

    async def interrupt(self, *, reason: str) -> None:
        return None


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


async def said_by(core: NoxCore) -> list[str]:
    speaker = RecordingSpeaker()
    core.orchestrator.speaker = speaker  # type: ignore[assignment]
    await core.orchestrator._say_failure("Kein Gehirn verfügbar.", "de", "r1")  # noqa: SLF001
    return speaker.said


async def test_the_failure_notice_is_spoken_when_nothing_forbids_it(core: NoxCore) -> None:
    assert await said_by(core) == ["Kein Gehirn verfügbar."]


async def test_the_failure_notice_is_spoken_in_a_private_privacy_mode_too(core: NoxCore) -> None:
    """No local model with the cloud off is exactly the case that must be heard."""
    await core.security.privacy.set_mode(PrivacyMode.PRIVATE, by="test")

    assert await said_by(core) == ["Kein Gehirn verfügbar."]


async def test_the_failure_notice_is_not_spoken_while_muted(core: NoxCore) -> None:
    await core.state.update("assistant.muted", True, reason="test")

    assert await said_by(core) == []


async def test_the_failure_notice_is_not_spoken_while_a_game_runs(core: NoxCore) -> None:
    await core.state.update("assistant.mode", Mode.ROCKET_LEAGUE, reason="test")

    assert await said_by(core) == []
