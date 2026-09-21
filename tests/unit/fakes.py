"""Shared in-memory fakes for contract-level tests (bus, state manager, router, speaker)."""

from __future__ import annotations

import asyncio
import fnmatch
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from nox.ai.base import AiChunk, AiRequest, AiResponse, ProviderInfo
from nox.core.events import Event, Handler
from nox.core.state import NoxState
from nox.voice.base import TtsRequest


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, *, interval: float = 0.02
) -> None:
    """Poll `predicate()` until it's true or `timeout` seconds pass.

    For tests that wait on a condition becoming true (a background task publishing an event,
    a client finishing a handshake) rather than on a fixed amount of time passing. Prefer this
    over `await asyncio.sleep(<magic number>)`: it returns as soon as the condition holds instead
    of always waiting the worst case, and it fails with a clear `AssertionError` instead of the
    test silently asserting on a state that never arrived. Do not use it for tests where the
    timing itself is the behaviour under test - those should assert on an injected clock instead.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


class FakeBus:
    def __init__(self) -> None:
        self.subs: list[tuple[str, Handler]] = []
        self.published: list[Event] = []

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        entry = (pattern, handler)
        self.subs.append(entry)
        return lambda: self.subs.remove(entry) if entry in self.subs else None

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        for pattern, handler in list(self.subs):
            if fnmatch.fnmatchcase(event.name, pattern):
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result

    async def fire(self, name: str, payload: dict[str, Any] | None = None) -> None:
        """Convenience for tests that don't need a full `Event` literal."""
        await self.publish(Event(name=name, payload=payload or {}))

    async def wait_for(
        self, name: str, *, timeout: float | None = None, corr: str | None = None
    ) -> Event:
        fut: asyncio.Future[Event] = asyncio.get_running_loop().create_future()

        def _h(ev: Event) -> None:
            if not fut.done() and (corr is None or ev.corr == corr):
                fut.set_result(ev)

        unsub = self.subscribe(name, _h)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            unsub()

    def names(self) -> list[str]:
        return [e.name for e in self.published]

    def of(self, name: str) -> list[Event]:
        """All published events with this exact name, in publish order."""
        return [e for e in self.published if e.name == name]


class MutableClock:
    """A hand-wound wall-clock double: nothing under test may depend on real time passing.

    Returns timezone-aware `datetime`s; advance it explicitly instead of sleeping.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# Some call sites read better with the plain name; same type.
Clock = MutableClock


class MonotonicClock:
    """A hand-wound float clock for code built on `time.monotonic`-shaped callables (deadlines,
    health-cache decay) rather than wall-clock timestamps.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeState:
    def __init__(self) -> None:
        self._state = NoxState()
        self.updates: list[tuple[str, Any, str]] = []

    @property
    def state(self) -> NoxState:
        return self._state

    def get(self, path: str) -> Any:
        if not path:
            return self._state
        obj: Any = self._state
        for part in path.split("."):
            obj = getattr(obj, part) if hasattr(obj, part) else obj[part]
        return obj

    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        self.updates.append((path, value, reason))
        data = self._state.model_dump()
        cur = data
        parts = path.split(".")
        for part in parts[:-1]:
            cur = cur[part]
        cur[parts[-1]] = value
        data["version"] = data["version"] + 1
        self._state = NoxState.model_validate(data)
        return self._state.version

    async def checkpoint(self, *, immediate: bool = False) -> None:
        return None

    async def restore_latest(self) -> bool:
        return False

    def snapshot(self) -> dict[str, Any]:
        return self._state.model_dump(mode="json")


class FakeRouter:
    def __init__(
        self,
        bus: FakeBus,
        text: str = "Hallo. Ich bin Nox! Wie geht es dir?",
        provider: str = "fake",
    ) -> None:
        self.bus = bus
        self.text = text
        self.provider = provider
        self.requests: list[AiRequest] = []
        self.delay = 0.0

    async def complete(self, request: AiRequest) -> AiResponse:
        chunks = [c.delta async for c in self.stream(request)]
        return AiResponse(
            request_id=request.request_id,
            provider=self.provider,
            text="".join(chunks),
            latency_ms=1,
        )

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        self.requests.append(request)
        await self.bus.publish(
            Event(
                name="ai.request_started",
                payload={
                    "request_id": request.request_id,
                    "provider": self.provider,
                    "role": request.role.value,
                    "mode": request.mode,
                },
            )
        )
        words = self.text.split(" ")
        for i, word in enumerate(words):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield AiChunk(
                request_id=request.request_id, delta=word + (" " if i < len(words) - 1 else "")
            )
        await self.bus.publish(
            Event(
                name="ai.response_ready",
                payload={
                    "request_id": request.request_id,
                    "provider": self.provider,
                    "text": self.text,
                    "latency_ms": 1,
                    "degraded": self.provider == "rules",
                },
            )
        )
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    async def providers(self) -> list[ProviderInfo]:
        return []

    def explain(self, request_id: str) -> str:
        return f"{self.provider} chosen (fake)"


class FakeSpeaker:
    def __init__(self) -> None:
        self.said: list[TtsRequest] = []
        self.interrupts: list[str] = []

    async def say(self, request: TtsRequest) -> None:
        self.said.append(request)

    async def interrupt(self, *, reason: str) -> None:
        self.interrupts.append(reason)


class FakeTurns:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    async def record(
        self, session_id: str, role: str, text: str, *, provider: str = "", latency_ms: int = 0
    ) -> None:
        self.rows.append((session_id, role, text))

    async def recent(self, session_id: str, limit: int) -> list[tuple[str, str]]:
        return [(r, t) for s, r, t in self.rows if s == session_id][-limit:]
