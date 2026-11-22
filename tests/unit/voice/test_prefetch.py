"""Synthesising a sentence ahead of its turn: ordering, the buffer's limit, cancelling, errors.

Nothing here waits for time. The engine is an async generator the test holds by events, and "the
machine is busy" is a number the test returns.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

import pytest

from nox.voice.prefetch import PrefetchedSpeech, ResourceGate, system_cpu_load
from tests.unit.voice.conftest import settle


class Source:
    """An engine stream of `count` four-byte chunks that records how far it was pulled."""

    def __init__(self, count: int = 5, *, fail_after: int | None = None) -> None:
        self.count = count
        self.fail_after = fail_after
        self.pulled = 0
        self.created = 0
        self.closed = False
        self.gate: asyncio.Event | None = None

    def __call__(self) -> AsyncIterator[bytes]:
        self.created += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[bytes]:
        try:
            for i in range(self.count):
                if self.gate is not None:
                    await self.gate.wait()
                if self.fail_after is not None and i == self.fail_after:
                    raise ValueError("the engine broke")
                self.pulled += 1
                yield bytes([i]) * 4
        finally:
            self.closed = True


async def read_all(speech: PrefetchedSpeech) -> list[bytes]:
    return [chunk async for chunk in speech.chunks()]


async def test_the_sentence_comes_out_in_order_and_ends() -> None:
    source = Source(5)
    speech = PrefetchedSpeech(source, max_bytes=1000)

    assert await read_all(speech) == [bytes([i]) * 4 for i in range(5)]
    assert speech.finished and source.closed


async def test_the_engine_is_not_touched_before_the_synthesis_starts() -> None:
    source = Source()
    speech = PrefetchedSpeech(source, max_bytes=1000)

    await settle()

    assert source.created == 0 and not speech.started


async def test_audio_is_ready_before_anyone_asks_for_it() -> None:
    source = Source(3)
    speech = PrefetchedSpeech(source, max_bytes=1000)
    speech.start()
    await settle()

    assert speech.buffered_bytes == 12 and speech.finished  # all of it, ahead of its turn
    assert await read_all(speech) == [bytes([i]) * 4 for i in range(3)]
    assert speech.was_ahead is True
    assert speech.buffered_bytes == 0


async def test_a_sentence_that_was_not_prefetched_starts_when_it_is_read() -> None:
    source = Source(2)
    speech = PrefetchedSpeech(source, max_bytes=1000)

    assert await read_all(speech) == [b"\x00" * 4, b"\x01" * 4]
    assert speech.was_ahead is False  # nothing was waiting for the listener


async def test_a_slow_listener_holds_the_synthesis_back_at_the_limit() -> None:
    source = Source(10)
    speech = PrefetchedSpeech(source, max_bytes=8)  # two chunks
    speech.start()
    await settle()

    assert speech.buffered_bytes == 8
    assert source.pulled <= 3  # the engine is not run to its end, only a chunk past the limit

    stream = speech.chunks()
    assert await anext(stream) == b"\x00" * 4  # one chunk read: room for more
    await settle()
    assert speech.buffered_bytes == 8 and source.pulled <= 4

    rest = [chunk async for chunk in stream]
    assert len(rest) == 9 and source.pulled == 10


async def test_cancelling_stops_the_engine_and_throws_the_audio_away() -> None:
    source = Source(10)
    source.gate = asyncio.Event()
    speech = PrefetchedSpeech(source, max_bytes=1000)
    speech.start()
    await settle()
    source.gate.set()
    await settle(2)  # a little audio is there ...

    await speech.cancel()

    assert source.closed  # ... and the engine's stream is closed, not abandoned
    assert speech.buffered_bytes == 0 and speech.cancelled
    assert await read_all(speech) == []  # a cancelled sentence is silence


async def test_cancel_nowait_works_without_awaiting_and_more_than_once() -> None:
    source = Source(10)
    source.gate = asyncio.Event()
    speech = PrefetchedSpeech(source, max_bytes=1000)
    speech.start()
    await settle()

    speech.cancel_nowait()
    speech.cancel_nowait()
    await settle()

    assert source.closed and speech.buffered_bytes == 0


async def test_a_reader_that_is_waiting_is_woken_by_the_cancel_and_ends() -> None:
    source = Source(10)
    source.gate = asyncio.Event()  # the engine never delivers
    speech = PrefetchedSpeech(source, max_bytes=1000)
    reader = asyncio.create_task(read_all(speech))
    await settle()
    assert not reader.done()

    speech.cancel_nowait()

    assert await asyncio.wait_for(reader, 1) == []


async def test_cancelling_a_sentence_that_never_started_never_starts_it() -> None:
    source = Source()
    speech = PrefetchedSpeech(source, max_bytes=1000)

    await speech.cancel()
    speech.start()
    await settle()

    assert source.created == 0
    assert await read_all(speech) == []
    assert source.created == 0


async def test_an_engine_error_arrives_after_the_audio_that_came_before_it() -> None:
    source = Source(5, fail_after=2)
    speech = PrefetchedSpeech(source, max_bytes=1000)
    speech.start()
    await settle()
    heard: list[bytes] = []

    with pytest.raises(ValueError, match="the engine broke"):
        async for chunk in speech.chunks():
            heard.append(chunk)

    assert heard == [b"\x00" * 4, b"\x01" * 4]  # the two good chunks were not lost to the error
    assert source.closed


async def test_the_buffer_needs_room_for_at_least_one_byte() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        PrefetchedSpeech(Source(), max_bytes=0)


# ---- the machine has room ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("load", "free"),
    [(0.0, True), (0.5, True), (0.849, True), (0.85, False), (0.99, False), (None, True)],
)
def test_the_look_ahead_waits_for_a_machine_with_room(load: float | None, free: bool) -> None:
    assert ResourceGate(0.85, lambda: load).free() is free


def test_a_probe_that_fails_does_not_switch_the_look_ahead_off() -> None:
    def broken() -> float:
        raise OSError("counter unavailable")

    assert ResourceGate(0.85, broken).free() is True


@pytest.mark.parametrize("ceiling", [0.0, -0.1, 1.5])
def test_a_ceiling_outside_zero_to_one_is_refused(ceiling: float) -> None:
    with pytest.raises(ValueError, match="ceiling"):
        ResourceGate(ceiling, lambda: 0.0)


def test_the_system_load_is_a_fraction_or_unknown() -> None:
    load = system_cpu_load()

    assert load is None or 0.0 <= load <= 1.0


def test_the_system_load_is_unknown_without_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # an import of it now fails

    assert system_cpu_load() is None
