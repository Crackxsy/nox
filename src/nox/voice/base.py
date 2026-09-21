"""Voice contracts: STT, TTS, audio capture and routing.

Local-first, with pluggable engines behind each protocol. The voice layer is never a security
boundary: it emits events and requests, and the core decides what may happen.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol

import numpy as np
from pydantic import BaseModel

from nox.core.events import HealthStatus


class Channel(StrEnum):
    PRIVATE = "private"  # user's headphones only
    STREAM = "stream"  # stream audio only (e.g. a virtual audio cable)
    BOTH = "both"
    MUTE = "mute"


class Transcript(BaseModel):
    text: str
    language: str
    confidence: float
    duration_ms: int
    latency_ms: int
    partial: bool = False


class SttEngine(Protocol):
    id: str

    async def health(self) -> tuple[HealthStatus, str]: ...
    async def load(self) -> None: ...
    async def transcribe(
        self, audio: np.ndarray, sample_rate: int, *, language: str = "auto"
    ) -> Transcript: ...
    async def unload(self) -> None: ...


class TtsRequest(BaseModel):
    utterance_id: str
    text: str
    language: str = "de"
    voice: str = ""
    rate: float = 1.0
    channel: Channel = Channel.PRIVATE
    prepared_clip: str | None = None  # id of a pre-rendered clip (RL callouts) bypasses synthesis


class TtsEngine(Protocol):
    id: str

    async def health(self) -> tuple[HealthStatus, str]: ...
    async def load(self) -> None: ...
    def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        """Yields PCM16 chunks (rate = `sample_rate`) as soon as they are ready (streaming)."""
        ...

    @property
    def sample_rate(self) -> int: ...
    async def unload(self) -> None: ...


class AudioOutput(Protocol):
    """Routes PCM to the private and/or stream device. Supports immediate stop for barge-in."""

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None: ...
    async def stop(self, *, reason: str) -> None: ...
    def devices(self) -> list[dict[str, str]]: ...


class AudioInput(Protocol):
    """Microphone capture. Frames flow only while capture is permitted (privacy state).

    `enabled` is the capture gate itself, not a filter in front of one: an implementation backed
    by real hardware holds no open capture stream while it is False, so the operating system's
    microphone indicator is off. Opening a device is blocking work, hence `set_enabled` is async.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def frames(self) -> AsyncIterator[np.ndarray]: ...
    @property
    def sample_rate(self) -> int: ...
    @property
    def enabled(self) -> bool: ...
    async def set_enabled(self, value: bool) -> None: ...


class VoicePipeline(Protocol):
    """Wake word / push-to-talk -> STT -> (core decides) -> TTS -> output, with barge-in.

    Must never capture while `privacy.microphone` is False or the kill switch is engaged;
    `refresh_gate` is how the owner re-applies that decision after either one changes.
    """

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def push_to_talk(self, pressed: bool) -> None: ...
    async def set_muted(self, muted: bool) -> None: ...
    async def say(self, request: TtsRequest) -> None: ...
    async def interrupt(self, *, reason: str) -> None: ...
    async def refresh_gate(self) -> None: ...
    def capture_health(self) -> tuple[HealthStatus, str]: ...
