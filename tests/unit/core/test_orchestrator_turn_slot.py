"""The one turn slot (`Orchestrator.handle_text`): barge-ins, waiting turns and cancellation.

C10 quality wave: a waiting node turn never runs ahead of the owner's new input, the newer of two
barge-ins in one cancel window wins, a barge-in whose caller gave up starts nothing, and the slot
is not handed on before a cancelled turn has finished unwinding.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.unit.core.test_orchestrator import make

NODE = "voice:node:" + "a" * 32


def _asked(router: Any) -> list[str]:
    """Which prompts reached the model, in order (by the topic word of the test prompts)."""
    topics = ("Python", "Rust", "Go", "Java")
    found: list[str] = []
    for request in router.requests:
        text = request.messages[-1].content
        found.append(next((topic for topic in topics if topic in text), "?"))
    return found


def _slow_interrupt(speaker: Any, delay: float = 0.05) -> None:
    async def interrupt(*, reason: str) -> None:
        speaker.interrupts.append(reason)
        await asyncio.sleep(delay)  # a speaker that yields while it stops: the cancel window

    speaker.interrupt = interrupt


async def test_a_waiting_node_turn_yields_to_a_pending_barge_in() -> None:
    orch, _bus, router, _speaker, _turns = make(speak=False)
    router.delay = 0.02
    await orch.start()
    first = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:a"))
    await asyncio.sleep(0.03)
    node = asyncio.create_task(
        orch.handle_text("Erklär mir kurz Rust", channel=NODE, barge_in=False)
    )
    await asyncio.sleep(0.01)  # the node's turn waits for the slot
    owner = asyncio.create_task(orch.handle_text("Erklär mir kurz Go", channel="chat:b"))
    results = await asyncio.gather(first, node, owner, return_exceptions=True)
    # The owner's new input never queues behind a room's question: it goes first.
    assert _asked(router) == ["Python", "Go", "Rust"]
    assert isinstance(results[0], asyncio.CancelledError)  # barged
    assert results[1].response and results[2].response
    await orch.stop()


async def test_owner_input_cancels_a_running_node_turn() -> None:
    orch, _bus, router, _speaker, _turns = make(speak=False)
    router.delay = 0.02
    await orch.start()
    node = asyncio.create_task(
        orch.handle_text("Erklär mir kurz Rust", channel=NODE, barge_in=False)
    )
    await asyncio.sleep(0.03)
    owner = await orch.handle_text("Erklär mir kurz Go", channel="chat:b")
    assert owner.response
    with pytest.raises(asyncio.CancelledError):
        await node
    assert _asked(router) == ["Rust", "Go"]
    await orch.stop()


async def test_of_two_barge_ins_in_one_cancel_window_the_newer_wins() -> None:
    orch, _bus, router, speaker, _turns = make(speak=False)
    router.delay = 0.02
    _slow_interrupt(speaker)
    await orch.start()
    first = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:a"))
    await asyncio.sleep(0.1)  # its own start (one slow interrupt) is over: it streams
    assert _asked(router) == ["Python"]
    second = asyncio.create_task(orch.handle_text("Erklär mir kurz Rust", channel="chat:a"))
    await asyncio.sleep(0.01)  # still cancelling the first: inside the cancel window
    third = asyncio.create_task(orch.handle_text("Erklär mir kurz Go", channel="chat:a"))
    loop = asyncio.get_running_loop()
    started = loop.time()
    results = await asyncio.gather(first, second, third, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], asyncio.CancelledError)  # superseded before it started
    assert results[2].response.startswith("Hallo.")
    assert _asked(router) == ["Python", "Go"]  # the second never reached the model
    # The newest input did not wait for the second one's whole answer.
    assert loop.time() - started < 0.5
    await orch.stop()


async def test_a_barge_in_whose_caller_gave_up_starts_no_turn() -> None:
    orch, _bus, router, _speaker, _turns = make(speak=False)
    unwound = asyncio.Event()
    stream = router.stream

    async def slow_to_unwind(request: Any) -> AsyncIterator[Any]:
        try:
            async for chunk in stream(request):
                await asyncio.sleep(0.02)
                yield chunk
        finally:
            await asyncio.sleep(0.05)  # a provider that takes a moment to let go
            unwound.set()

    router.stream = slow_to_unwind
    await orch.start()
    first = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:a"))
    await asyncio.sleep(0.03)
    second = asyncio.create_task(orch.handle_text("Erklär mir kurz Rust", channel="chat:b"))
    await asyncio.sleep(0.01)  # it is waiting for the first turn to unwind
    second.cancel()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    assert unwound.is_set()
    await asyncio.sleep(0.05)
    assert _asked(router) == ["Python"]  # the abandoned barge-in started nothing
    assert not orch._slot.locked()  # noqa: SLF001 - and left the slot free
    await orch.stop()


async def test_the_slot_is_held_until_a_cancelled_turn_has_unwound() -> None:
    orch, _bus, router, _speaker, _turns = make(speak=False)
    events: list[str] = []
    stream = router.stream

    async def slow_to_unwind(request: Any) -> AsyncIterator[Any]:
        events.append("start:" + _asked_one(request))
        try:
            async for chunk in stream(request):
                await asyncio.sleep(0.02)
                yield chunk
        finally:
            if "Python" in request.messages[-1].content:
                await asyncio.sleep(0.05)
                events.append("unwound:Python")

    def _asked_one(request: Any) -> str:
        return "Python" if "Python" in request.messages[-1].content else "Rust"

    router.stream = slow_to_unwind
    await orch.start()
    outer = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:a"))
    await asyncio.sleep(0.03)
    node = asyncio.create_task(
        orch.handle_text("Erklär mir kurz Rust", channel=NODE, barge_in=False)
    )
    await asyncio.sleep(0.01)
    outer.cancel()  # the caller of the running turn gives up ...
    await asyncio.sleep(0.01)
    outer.cancel()  # ... and once more while the turn is still letting go
    results = await asyncio.gather(outer, node, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError) and results[1].response
    assert events == ["start:Python", "unwound:Python", "start:Rust"]
    await orch.stop()
