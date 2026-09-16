"""nox.core.bus: glob matching, ordering, priority lanes, isolation, validation, backpressure."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from nox.core.bus import AsyncEventBus, compile_pattern
from nox.core.events import E, Event


@pytest.mark.parametrize(
    ("pattern", "name", "expected"),
    [
        ("voice.*", "voice.transcript_ready", True),
        ("voice.*", "voice.a.b", False),
        ("voice.*", "tts.started", False),
        ("*", "voice.transcript_ready", True),
        ("**", "a.b.c", True),
        ("voice.**", "voice.a.b", True),
        ("voice.**", "voice", False),
        ("voice.transcript_*", "voice.transcript_partial", True),
        ("voice.transcript_*", "voice.ptt_pressed", False),
        ("*.changed", "state.changed", True),
        ("*.changed", "system.mode_changed", False),
        ("state.changed", "state.changed", True),
        ("state.changed", "state.changed.x", False),
    ],
)
def test_glob_patterns(pattern: str, name: str, expected: bool) -> None:
    assert bool(compile_pattern(pattern).match(name)) is expected


def test_bad_patterns_rejected() -> None:
    with pytest.raises(ValueError):
        compile_pattern("")
    with pytest.raises(ValueError):
        compile_pattern("a..b")


async def test_sync_and_async_handlers_and_unsubscribe() -> None:
    bus = AsyncEventBus()
    seen: list[str] = []

    def sync_handler(ev: Event) -> None:
        seen.append("sync:" + ev.name)

    async def async_handler(ev: Event) -> None:
        await asyncio.sleep(0)
        seen.append("async:" + ev.name)

    unsub = bus.subscribe("pet.*", sync_handler)
    bus.subscribe("pet.*", async_handler)
    await bus.publish(Event(name="pet.interaction", payload={"kind": "click"}))
    assert seen == ["sync:pet.interaction", "async:pet.interaction"]
    unsub()
    await bus.publish(Event(name="pet.interaction"))
    assert seen == ["sync:pet.interaction", "async:pet.interaction", "async:pet.interaction"]
    assert bus.stats().subscriptions == 1


async def test_priority_handlers_run_first() -> None:
    bus = AsyncEventBus()
    order: list[str] = []
    bus.subscribe("**", lambda ev: order.append("catch_all"))
    bus.subscribe("security.kill_switch", lambda ev: order.append("exact"))
    bus.subscribe("security.*", lambda ev: order.append("security"))
    bus.subscribe("system.*", lambda ev: order.append("system"))
    await bus.publish(Event(name=E.SECURITY_KILL_SWITCH, payload={"by": "hotkey"}))
    assert order == ["exact", "security", "catch_all"]


async def test_handler_isolation_counts_and_emits_failure() -> None:
    bus = AsyncEventBus()
    delivered: list[str] = []
    failures: list[Event] = []

    def boom(ev: Event) -> None:
        raise RuntimeError("handler broke")

    bus.subscribe("voice.*", boom)
    bus.subscribe("voice.*", lambda ev: delivered.append(ev.name))
    bus.subscribe(E.SYSTEM_HANDLER_FAILED, failures.append)
    await bus.publish(Event(name="voice.muted"))  # must not raise
    assert delivered == ["voice.muted"]
    stats = bus.stats()
    assert stats.handler_errors == 1
    assert list(stats.errors_by_handler.values()) == [1]
    assert len(failures) == 1
    assert failures[0].payload["event"] == "voice.muted"
    assert "handler broke" in failures[0].payload["error"]


async def test_failure_in_failure_handler_does_not_recurse() -> None:
    bus = AsyncEventBus()

    def boom(ev: Event) -> None:
        raise RuntimeError("always")

    bus.subscribe("**", boom)
    await bus.publish(Event(name="voice.muted"))
    assert bus.stats().handler_errors == 2  # voice.muted + system.handler_failed, then stop


async def test_payload_validation() -> None:
    bus = AsyncEventBus()
    received: list[Event] = []
    bus.subscribe(E.STATE_CHANGED, received.append)
    with pytest.raises(ValidationError):
        await bus.publish(Event(name=E.STATE_CHANGED, payload={"path": "x"}))  # version missing
    assert received == []
    await bus.publish(Event(name=E.STATE_CHANGED, payload={"path": "x", "version": 1}))
    assert received[0].payload == {"path": "x", "old": None, "new": None, "version": 1}
    await bus.publish(Event(name="custom.unknown", payload={"anything": object()}))  # passthrough


async def test_wait_for_with_corr_and_timeout() -> None:
    bus = AsyncEventBus()

    async def later() -> None:
        await asyncio.sleep(0.01)
        await bus.publish(Event(name="ai.response_chunk", corr="other"))
        await bus.publish(Event(name="ai.response_chunk", corr="mine", payload={"n": 1}))

    task = asyncio.create_task(later())
    ev = await bus.wait_for("ai.*", timeout=1.0, corr="mine")
    assert ev.payload == {"n": 1}
    await task
    with pytest.raises(TimeoutError):
        await bus.wait_for("never.happens", timeout=0.02)
    assert bus.stats().subscriptions == 0


async def test_reentrant_publish_from_handler() -> None:
    bus = AsyncEventBus()
    seen: list[str] = []

    async def first(ev: Event) -> None:
        seen.append(ev.name)
        await bus.publish(Event(name="b.second"))

    bus.subscribe("a.first", first)
    bus.subscribe("b.second", lambda ev: seen.append(ev.name))
    await bus.publish(Event(name="a.first"))
    assert seen == ["a.first", "b.second"]


async def test_backpressure_and_drop_oldest_for_stream_family() -> None:
    bus = AsyncEventBus(capacity=1)
    gate = asyncio.Event()
    delivered: list[str] = []

    async def slow(ev: Event) -> None:
        await gate.wait()
        delivered.append(ev.name)

    bus.subscribe("**", slow)
    blocker = asyncio.create_task(bus.publish(Event(name="pet.blocker")))
    await asyncio.sleep(0)  # blocker now in flight, capacity exhausted
    s1 = asyncio.create_task(bus.publish(Event(name="stream.frame", payload={"i": 1})))
    await asyncio.sleep(0)
    assert bus.stats().waiting == 1
    s2 = asyncio.create_task(bus.publish(Event(name="stream.frame", payload={"i": 2})))
    await asyncio.sleep(0)
    await s1  # s1 was dropped (oldest droppable) and returned without delivery
    assert bus.stats().dropped == 1
    # "pet.interaction" here is an arbitrary normal-priority, non-droppable event name (like
    # "pet.blocker" above); not "memory.created", which now has a registered payload model
    # (nox.core.events.MemoryCreated, EPIC-07) an empty payload would fail validation against.
    normal = asyncio.create_task(bus.publish(Event(name="pet.interaction")))
    await asyncio.sleep(0)
    assert bus.stats().waiting == 2  # s2 and the normal event both wait (backpressure)
    gate.set()
    await asyncio.gather(blocker, s2, normal)
    assert delivered == ["pet.blocker", "stream.frame", "pet.interaction"]
    assert bus.stats().in_flight == 0 and bus.stats().waiting == 0


async def test_priority_lane_bypasses_capacity() -> None:
    bus = AsyncEventBus(capacity=1)
    gate = asyncio.Event()
    delivered: list[str] = []

    async def slow(ev: Event) -> None:
        if ev.name == "pet.blocker":
            await gate.wait()
        delivered.append(ev.name)

    bus.subscribe("**", slow)
    blocker = asyncio.create_task(bus.publish(Event(name="pet.blocker")))
    await asyncio.sleep(0)
    await asyncio.wait_for(bus.publish(Event(name="system.stopping")), 0.5)  # not blocked
    assert delivered == ["system.stopping"]
    gate.set()
    await blocker
