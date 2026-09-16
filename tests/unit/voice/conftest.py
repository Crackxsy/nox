"""Shared fakes for voice tests: in-memory audio in/out, STT, TTS and IPC client. No hardware."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

import numpy as np
import pytest

from nox.core.events import HealthStatus
from nox.ipc.protocol import Envelope, Kind, Source
from nox.voice.base import Channel, Transcript, TtsRequest

SR = 16000
FRAME = SR * 30 // 1000


def tone(ms: int, *, freq: float = 220.0, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    n = sr * ms // 1000
    t = np.arange(n, dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(ms: int, *, amp: float = 0.001, sr: int = SR, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(sr * ms // 1000) * amp).astype(np.float32)


def frames_of(signal: np.ndarray, frame: int = FRAME) -> list[np.ndarray]:
    full = signal.size // frame
    return [signal[i * frame : (i + 1) * frame] for i in range(full)]


class FakeAudioInput:
    """Frames are pushed by the test; only delivered while `enabled` (like SoundDeviceInput)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
        self.enabled = False
        self.started = False
        self.stopped = False
        self.delivered = 0

    @property
    def sample_rate(self) -> int:
        return SR

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.queue.put_nowait(None)

    def push(self, frame: np.ndarray, *, force: bool = False) -> None:
        if self.enabled or force:
            self.delivered += 1
            self.queue.put_nowait(frame)

    def push_all(self, frames: Iterable[np.ndarray], *, force: bool = False) -> None:
        for f in frames:
            self.push(f, force=force)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        while True:
            frame = await self.queue.get()
            if frame is None:
                return
            yield frame


class FakeAudioOutput:
    def __init__(self, *, chunk_delay: float = 0.0) -> None:
        self.played: list[tuple[str, Channel, list[bytes]]] = []
        self.stops: list[str] = []
        self.chunk_delay = chunk_delay
        self._stopped = asyncio.Event()

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        self._stopped.clear()
        chunks: list[bytes] = []
        self.played.append((utterance_id, channel, chunks))
        async for chunk in pcm_chunks:
            if self._stopped.is_set():
                break
            chunks.append(chunk)
            if self.chunk_delay:
                await asyncio.sleep(self.chunk_delay)

    async def stop(self, *, reason: str) -> None:
        self.stops.append(reason)
        self._stopped.set()

    def devices(self) -> list[dict[str, str]]:
        return []


class FakeStt:
    id = "fake-stt"

    def __init__(self, text: str = "Nox wie spät ist es", *, language: str = "de") -> None:
        self.text = text
        self.language = language
        self.calls: list[np.ndarray] = []
        self.delay = 0.0

    async def health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.AVAILABLE, "fake"

    async def load(self) -> None:
        return None

    async def transcribe(
        self, audio: np.ndarray, sample_rate: int, *, language: str = "auto"
    ) -> Transcript:
        self.calls.append(audio)
        if self.delay:
            await asyncio.sleep(self.delay)
        return Transcript(
            text=self.text,
            language=self.language,
            confidence=0.9,
            duration_ms=int(audio.size * 1000 / sample_rate),
            latency_ms=5,
        )

    async def unload(self) -> None:
        return None


class FakeTts:
    id = "fake-tts"

    def __init__(self, *, chunks: int = 3, delay: float = 0.0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.requests: list[TtsRequest] = []
        self.closed = 0

    async def health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.AVAILABLE, "fake"

    async def load(self) -> None:
        return None

    @property
    def sample_rate(self) -> int:
        return 22050

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        self.requests.append(request)
        try:
            for i in range(self.chunks):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield bytes([i]) * 4
        finally:
            self.closed += 1

    async def unload(self) -> None:
        return None


class FakeIpcClient:
    """Records outbound traffic and lets tests deliver inbound requests/events (IpcClient shape)."""

    def __init__(self, *, register_response: dict[str, Any] | None = None) -> None:
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.subscriptions: list[str] = []
        self.inbound: dict[str, Any] = {}
        self.event_handlers: list[tuple[str, Callable[[Envelope], Awaitable[None] | None]]] = []
        self.register_response = register_response or {"ok": True, "config": {}}
        self.fail_requests = False

    async def connect(self) -> Any:
        self.connected = True
        return None

    async def close(self) -> None:
        self.closed = True

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if self.fail_requests:
            raise RuntimeError("hub down")
        self.requests.append((name, dict(payload or {})))
        if name == "worker.register":
            return self.register_response
        return {"ok": True}

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        self.events.append((name, dict(payload or {})))

    async def subscribe(self, patterns: Iterable[str], handler: Any = None) -> list[str]:
        self.subscriptions.extend(patterns)
        return list(self.subscriptions)

    def on(self, name_glob: str, handler: Callable[[Envelope], Awaitable[None] | None]) -> Any:
        self.event_handlers.append((name_glob, handler))
        return lambda: None

    def handle(self, name: str, handler: Any) -> None:
        self.inbound[name] = handler

    # -- test helpers --------------------------------------------------------------------------

    async def deliver_request(self, name: str, payload: dict[str, Any]) -> Any:
        return await self.inbound[name](payload, None)

    async def deliver_event(self, name: str, payload: dict[str, Any]) -> None:
        env = Envelope(
            kind=Kind.EVENT, name=name, src=Source(role="core", id="core"), payload=payload
        )
        for glob, handler in self.event_handlers:
            if glob == name:
                result = handler(env)
                if asyncio.iscoroutine(result):
                    await result

    def event_names(self) -> list[str]:
        return [n for n, _ in self.events]


@pytest.fixture
def audio_in() -> FakeAudioInput:
    return FakeAudioInput()


@pytest.fixture
def audio_out() -> FakeAudioOutput:
    return FakeAudioOutput()


async def settle(steps: int = 20) -> None:
    for _ in range(steps):
        await asyncio.sleep(0)
