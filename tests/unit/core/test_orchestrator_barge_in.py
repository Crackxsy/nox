"""A barge-in cancels the turn it belongs to - and only that one.

The voice worker reports an interruption for every sentence it drops, and a report can arrive
after the user has already moved on: the old turn is over, a new one is running. These tests pin
that the event names the sentence, that the sentence has to be one of the running turn's, and that
a cancellation decided for one turn is never carried out on its successor.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nox.core.events import E, Event
from nox.core.orchestrator import Orchestrator
from tests.unit.fakes import FakeBus, FakeRouter
from tests.unit.fakes import make_orchestrator as make

SLOW = 3600.0  # a turn that is still running when the test ends it


async def running(orch: Orchestrator, router: FakeRouter, text: str) -> asyncio.Task[Any]:
    """A turn that is in flight: the model has not answered and will not for the whole test."""
    router.delay = SLOW
    task = asyncio.create_task(orch.handle_text(text))
    for _ in range(200):
        if orch.active_request_id is not None and router.requests:
            return task
        await asyncio.sleep(0)
    raise AssertionError("the turn never started")


async def interrupted(bus: FakeBus, utterance_id: object, reason: str = "barge_in") -> None:
    await bus.publish(
        Event(name=E.TTS_INTERRUPTED, payload={"utterance_id": utterance_id, "reason": reason})
    )
    for _ in range(20):
        await asyncio.sleep(0)  # the cancel runs in a task of its own


async def test_an_interruption_of_a_sentence_of_the_running_turn_cancels_it() -> None:
    orch, bus, router, speaker, _ = make()
    await orch.start()
    task = await running(orch, router, "lange Antwort bitte")

    await interrupted(bus, f"{orch.active_request_id}:1a2b3c4d")

    assert task.cancelled() or task.done()
    assert speaker.interrupts[-1] == "barge_in"
    await orch.stop()


async def test_a_late_interruption_of_an_earlier_turn_does_not_cancel_the_next_one() -> None:
    orch, bus, router, speaker, _ = make()
    await orch.start()
    first = await orch.handle_text("Erklär mir kurz Python")  # answered and over
    old_utterance = speaker.said[0].utterance_id
    assert old_utterance.startswith(first.request_id + ":")
    task = await running(orch, router, "Und jetzt etwas Langes")
    assert orch.active_request_id != first.request_id

    await interrupted(bus, old_utterance)  # the worker's report about the old turn's sentence

    assert not task.done()
    assert "barge_in" not in speaker.interrupts
    await orch.stop()


@pytest.mark.parametrize(
    "utterance_id",
    ["capture-notice:1a2b3c4d", "proactive", "", None, 7, "other-turn:1", ":1", "1:"],
)
async def test_an_interruption_of_a_sentence_that_is_not_of_the_turn_is_ignored(
    utterance_id: object,
) -> None:
    orch, bus, router, speaker, _ = make()
    await orch.start()
    task = await running(orch, router, "lange Antwort bitte")

    await interrupted(bus, utterance_id)

    assert not task.done()
    assert "barge_in" not in speaker.interrupts
    await orch.stop()


async def test_an_interruption_without_any_utterance_is_ignored_too() -> None:
    orch, bus, router, speaker, _ = make()
    await orch.start()
    task = await running(orch, router, "lange Antwort bitte")

    await bus.publish(Event(name=E.TTS_INTERRUPTED, payload={"reason": "barge_in"}))
    for _ in range(20):
        await asyncio.sleep(0)

    assert not task.done() and "barge_in" not in speaker.interrupts
    await orch.stop()


async def test_a_cancellation_decided_for_a_finished_turn_spares_the_one_that_followed() -> None:
    """`_cancel_turn` runs in a task of its own, so by the time it runs the turn it was started
    for may be gone and another one running: it has to look again, not cancel whatever is there."""
    orch, _, router, speaker, _ = make()
    await orch.start()
    first = await running(orch, router, "erste Antwort")
    old_turn = orch._active  # noqa: SLF001 - the task a barge-in event would have named
    assert old_turn is not None
    await orch.cancel(reason="test")
    with pytest.raises(asyncio.CancelledError):
        await first

    second = await running(orch, router, "zweite Antwort")
    await orch._cancel_turn(old_turn, "barge_in")  # noqa: SLF001

    assert not second.done()
    assert "barge_in" not in speaker.interrupts
    await orch.stop()


async def test_the_turn_that_is_cancelled_by_a_matching_task_is_the_running_one() -> None:
    """The other half of the guard: for the turn that *is* running the cancel does go through."""
    orch, _, router, speaker, _ = make()
    await orch.start()
    task = await running(orch, router, "lange Antwort bitte")
    turn = orch._active  # noqa: SLF001
    assert turn is not None

    await orch._cancel_turn(turn, "barge_in")  # noqa: SLF001

    assert task.done() and speaker.interrupts[-1] == "barge_in"
    await orch.stop()


async def test_no_request_id_is_active_between_turns() -> None:
    orch, _, _, _, _ = make()
    await orch.start()
    assert orch.active_request_id is None

    await orch.handle_text("Erklär mir kurz Python")

    assert orch.active_request_id is None
    await orch.stop()


async def test_the_id_in_the_sentences_is_the_one_the_turn_reports() -> None:
    orch, _, _, speaker, _ = make()
    await orch.start()

    turn = await orch.handle_text("Erklär mir kurz Python")

    assert speaker.said and all(
        s.utterance_id.partition(":")[0] == turn.request_id for s in speaker.said
    )
    await orch.stop()
