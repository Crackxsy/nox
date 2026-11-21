"""The core decides whether a capture notice is spoken: it is unprompted speech.

The voice worker only reports `voice.capture_notice`. These tests run the core's end against the
real `SpeechPolicy`, so mute, quiet hours, privacy zones and quiet privacy modes are the real ones.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from nox.core.config import NoxConfig
from nox.core.events import E, Event
from nox.core.speech_policy import SpeechPolicy
from nox.voice.base import TtsRequest
from nox.voice.capture_notice import CaptureNoticeSpeaker
from tests.unit.fakes import FakeSpeaker, FakeState


def policy(
    *,
    muted: bool = False,
    zone: str | None = None,
    mode: str = "balanced",
    hour: int = 12,
) -> SpeechPolicy:
    state = FakeState()
    state.state.assistant.muted = muted
    moment = datetime(2026, 9, 13, hour, 0)
    return SpeechPolicy(
        state=state,
        config=NoxConfig(),
        active_zone=lambda: zone,
        privacy_mode=lambda: mode,
        clock=lambda: moment,
    )


def notice(kind: str) -> Event:
    return Event(name=E.VOICE_CAPTURE_NOTICE, payload={"notice": kind, "reason": "gone"})


def make(pol: SpeechPolicy, language: str = "de") -> tuple[CaptureNoticeSpeaker, FakeSpeaker]:
    speaker = FakeSpeaker()
    return CaptureNoticeSpeaker(policy=pol, speaker=speaker, language=lambda: language), speaker


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("deaf", "Ich höre dich gerade nicht."),
        ("recovered", "Ich höre dich wieder."),
        ("unstable", "Mein Mikrofon wackelt, ich höre dich vielleicht nicht durchgehend."),
    ],
)
async def test_an_allowed_notice_is_spoken_privately_in_german(kind: str, text: str) -> None:
    notices, speaker = make(policy())

    await notices.on_event(notice(kind))

    assert [(r.text, r.language, str(r.channel)) for r in speaker.said] == [(text, "de", "private")]


async def test_the_notice_follows_the_configured_language() -> None:
    notices, speaker = make(policy(), language="en")

    await notices.on_event(notice("deaf"))

    assert [(r.text, r.language) for r in speaker.said] == [("I can't hear you right now.", "en")]


@pytest.mark.parametrize(
    "silent",
    [
        policy(muted=True),
        policy(zone="bedroom"),
        policy(mode="private"),
        policy(mode="offline"),
        policy(hour=2),  # inside the default quiet hours
    ],
    ids=["muted", "privacy zone", "private mode", "offline mode", "quiet hours"],
)
async def test_nothing_is_spoken_where_unprompted_speech_is_not_allowed(
    silent: SpeechPolicy,
) -> None:
    notices, speaker = make(silent)

    await notices.on_event(notice("deaf"))

    assert speaker.said == []


async def test_a_speaker_that_fails_does_not_break_the_event_handler() -> None:
    class Broken:
        async def say(self, request: TtsRequest) -> None:
            raise OSError("worker gone")

    notices = CaptureNoticeSpeaker(policy=policy(), speaker=Broken(), language=lambda: "de")

    await notices.on_event(notice("deaf"))  # must not raise into the bus


async def test_a_malformed_notice_is_ignored() -> None:
    notices, speaker = make(policy())

    await notices.on_event(Event(name=E.VOICE_CAPTURE_NOTICE, payload={"notice": "boom"}))

    assert speaker.said == []
