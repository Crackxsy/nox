"""Saying that the microphone is gone, unstable or back - when, and only when, Nox may speak.

The voice worker notices the problem and reports it as `voice.capture_notice`; it does not speak it
itself, because an unprompted sentence is subject to the rules only the core knows: mute, quiet
hours, privacy mode and active privacy zones (`SpeechPolicy`). This is the core's end of that
event. When speech is not allowed nothing is said; the voice health line still shows the problem,
and that is where the user finds it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol

from nox.core.events import Event
from nox.core.events.system import VoiceCaptureNotice
from nox.core.speech_policy import SpeechKind
from nox.voice._logging import get_logger
from nox.voice.base import Channel, TtsRequest
from nox.voice.capture_health import CaptureNotice, notice_text

log = get_logger(__name__)

__all__ = ["CaptureNoticeSpeaker"]


class _Policy(Protocol):
    def may_speak(self, kind: SpeechKind) -> tuple[bool, str]: ...


class _Speaker(Protocol):
    async def say(self, request: TtsRequest) -> None: ...


class CaptureNoticeSpeaker:
    """Speaks a capture notice on the private channel if the speech policy allows it."""

    def __init__(self, *, policy: _Policy, speaker: _Speaker, language: Callable[[], str]) -> None:
        self._policy = policy
        self._speaker = speaker
        self._language = language

    async def on_event(self, event: Event) -> None:
        try:
            payload = VoiceCaptureNotice.model_validate(event.payload)
        except ValueError as exc:
            log.warning("voice.capture_notice_invalid", error=str(exc))
            return
        allowed, reason = self._policy.may_speak("proactive")
        if not allowed:
            log.info("voice.capture_notice_silent", notice=payload.notice, reason=reason)
            return
        language, text = notice_text(CaptureNotice(payload.notice), self._language())
        try:
            await self._speaker.say(
                TtsRequest(
                    utterance_id=f"capture-notice:{uuid.uuid4().hex[:8]}",
                    text=text,
                    language=language,
                    channel=Channel.PRIVATE,
                )
            )
        except Exception as exc:  # noqa: BLE001 - speech is a bonus; health already says it
            log.warning("voice.capture_notice_failed", error=f"{type(exc).__name__}: {exc}")
