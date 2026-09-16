"""OP-10: the startup-greeting path goes through `SpeechPolicy` (via `decide_greeting`) - it must
speak exactly once when allowed and never when muted. Deliberately does not boot a `NoxCore`
(too heavy for this decision); it wires only the minimal pieces `_greet_when_voice_ready` uses.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from nox.app import GREETING, decide_greeting
from nox.core.config import NoxConfig
from nox.core.speech_policy import SpeechPolicy
from nox.voice.base import TtsRequest
from tests.unit.fakes import FakeSpeaker, FakeState

NOON = datetime(2026, 9, 13, 12, 0)  # outside the default 23:00-08:00 quiet hours


def make_policy(*, muted: bool, config: NoxConfig | None = None) -> SpeechPolicy:
    state = FakeState()
    state.state.assistant.muted = muted
    return SpeechPolicy(
        state=state,
        config=config or NoxConfig(),
        active_zone=lambda: None,
        privacy_mode=lambda: "balanced",
        clock=lambda: NOON,
    )


async def greet_if_allowed(
    policy: SpeechPolicy, speaker: FakeSpeaker, *, language: str = "de"
) -> None:
    """The speak-or-not branch of `NoxCore._greet_when_voice_ready`, minus the worker-ready wait."""
    if not decide_greeting(policy):
        return
    await speaker.say(
        TtsRequest(
            utterance_id=f"greeting:{uuid.uuid4().hex[:8]}",
            text=GREETING.get(language, GREETING["en"]),
            language=language,
        )
    )


async def test_greeting_speaks_exactly_once_when_allowed() -> None:
    policy = make_policy(muted=False)
    speaker = FakeSpeaker()
    await greet_if_allowed(policy, speaker)
    assert len(speaker.said) == 1
    assert speaker.said[0].text == GREETING["de"]


async def test_greeting_never_speaks_when_muted() -> None:
    policy = make_policy(muted=True)
    speaker = FakeSpeaker()
    await greet_if_allowed(policy, speaker)
    assert speaker.said == []


async def test_greeting_never_speaks_when_disabled_in_config() -> None:
    cfg = NoxConfig.model_validate(
        {
            **NoxConfig().model_dump(mode="json"),
            "pet": {**NoxConfig().pet.model_dump(), "greeting_enabled": False},
        }
    )
    policy = make_policy(muted=False, config=cfg)
    speaker = FakeSpeaker()
    await greet_if_allowed(policy, speaker)
    assert speaker.said == []
