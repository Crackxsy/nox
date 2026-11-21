"""Interrupting speech ends the whole generation: playing sentence and queued ones alike.

The core hands sentences to the worker one request each, and each plays from its own task behind a
lock. Stopping only the sentence that plays used to leave every accepted follow-up to start right
after it, so Nox kept talking for seconds after being interrupted. These tests drive the state
machine directly: one sentence plays, others wait, an interruption lands, and nothing more may be
heard - while the next turn's sentences still play.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from nox.core.events import E
from nox.voice.base import Channel, TtsRequest
from nox.voice.pipeline import DefaultVoicePipeline
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt, FakeTts, settle


class HeldOutput(FakeAudioOutput):
    """Plays an utterance until the test releases it or the pipeline stops it."""

    def __init__(self) -> None:
        super().__init__()
        self.playing = asyncio.Event()
        self.release = asyncio.Event()

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        self._stopped.clear()
        self.played.append((utterance_id, channel, []))
        self.playing.set()
        waiters = {
            asyncio.ensure_future(self._stopped.wait()),
            asyncio.ensure_future(self.release.wait()),
        }
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in waiters:
            waiter.cancel()
        self.playing.clear()

    def played_ids(self) -> list[str]:
        return [utterance_id for utterance_id, _, _ in self.played]


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []
        #: Names of events that hold the emitter until the test sets the matching gate.
        self.gates: dict[str, asyncio.Event] = {}

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        self.items.append((name, payload))
        gate = self.gates.get(name)
        if gate is not None:
            await gate.wait()

    def of(self, name: str) -> list[dict[str, Any]]:
        return [payload for n, payload in self.items if n == name]

    def ids(self, name: str) -> list[str]:
        return [payload["utterance_id"] for payload in self.of(name)]


def make(
    audio_out: HeldOutput | None = None,
) -> tuple[DefaultVoicePipeline, HeldOutput, FakeTts, Events]:
    out = audio_out or HeldOutput()
    tts = FakeTts()
    events = Events()
    pipeline = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=out,
        stt=FakeStt(),
        tts=tts,
        emit=events,
        capture_allowed=lambda: True,
    )
    return pipeline, out, tts, events


def sentence(name: str) -> TtsRequest:
    return TtsRequest(utterance_id=name, text=f"Satz {name}")


async def test_sentences_waiting_behind_the_playing_one_are_dropped_by_an_interruption() -> None:
    pipeline, out, _, events = make()
    first = asyncio.create_task(pipeline.say(sentence("s1")))
    await out.playing.wait()
    queued = [asyncio.create_task(pipeline.say(sentence(n))) for n in ("s2", "s3")]
    await settle()

    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(first, *queued)

    assert out.played_ids() == ["s1"]  # s2 and s3 never reached the output
    assert events.ids(E.TTS_STARTED) == ["s1"]
    assert events.ids(E.TTS_INTERRUPTED) == ["s1", "s2", "s3"]
    assert events.of(E.TTS_FINISHED) == []  # every sentence ended exactly once, as interrupted


async def test_every_dropped_sentence_carries_the_reason_of_the_interruption() -> None:
    pipeline, out, _, events = make()
    first = asyncio.create_task(pipeline.say(sentence("s1")))
    await out.playing.wait()
    queued = asyncio.create_task(pipeline.say(sentence("s2")))
    await settle()

    await pipeline.interrupt(reason="kill_phrase")
    await asyncio.gather(first, queued)

    assert [p["reason"] for p in events.of(E.TTS_INTERRUPTED)] == ["kill_phrase", "kill_phrase"]


async def test_the_next_turn_speaks_normally_after_an_interruption() -> None:
    pipeline, out, _, events = make()
    first = asyncio.create_task(pipeline.say(sentence("old-1")))
    await out.playing.wait()
    old_queued = asyncio.create_task(pipeline.say(sentence("old-2")))
    await settle()
    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(first, old_queued)

    out.release.set()  # whatever plays from here on finishes by itself
    await pipeline.say(sentence("new-1"))
    await pipeline.say(sentence("new-2"))

    assert out.played_ids() == ["old-1", "new-1", "new-2"]
    assert events.ids(E.TTS_FINISHED) == ["new-1", "new-2"]
    assert events.ids(E.TTS_INTERRUPTED) == ["old-1", "old-2"]  # the old turn only


async def test_a_sentence_requested_right_after_an_interruption_belongs_to_the_next_turn() -> None:
    """The new turn's first sentence is queued behind the old ones; they drop, it plays."""
    pipeline, out, _, events = make()
    first = asyncio.create_task(pipeline.say(sentence("old-1")))
    await out.playing.wait()
    old_queued = asyncio.create_task(pipeline.say(sentence("old-2")))
    await settle()

    await pipeline.interrupt(reason="barge_in")
    out.release.set()
    new = asyncio.create_task(pipeline.say(sentence("new-1")))  # no await in between
    await asyncio.gather(first, old_queued, new)

    assert out.played_ids() == ["old-1", "new-1"]
    assert events.ids(E.TTS_FINISHED) == ["new-1"]


async def test_an_interruption_between_two_sentences_still_drops_the_waiting_one() -> None:
    """The gap: the first sentence has stopped playing and is reporting its end, the second waits.

    Nothing is "speaking" at that instant, which is when an interruption used to be ignored.
    """
    pipeline, out, _, events = make()
    out.release.set()
    events.gates[E.TTS_FINISHED] = asyncio.Event()
    first = asyncio.create_task(pipeline.say(sentence("s1")))
    await settle()
    assert events.ids(E.TTS_FINISHED) == ["s1"]  # held inside its terminal event, lock still taken
    waiting = asyncio.create_task(pipeline.say(sentence("s2")))
    await settle()
    assert pipeline.speaking is False

    await pipeline.interrupt(reason="barge_in")
    events.gates[E.TTS_FINISHED].set()
    await asyncio.gather(first, waiting)

    assert out.played_ids() == ["s1"]
    assert events.ids(E.TTS_INTERRUPTED) == ["s2"]


async def test_an_interruption_while_tts_started_is_in_flight_prevents_playback() -> None:
    pipeline, out, _, events = make()
    events.gates[E.TTS_STARTED] = asyncio.Event()
    task = asyncio.create_task(pipeline.say(sentence("s1")))
    await settle()
    assert events.ids(E.TTS_STARTED) == ["s1"]

    await pipeline.interrupt(reason="barge_in")
    events.gates[E.TTS_STARTED].set()
    await task

    assert out.played == []  # the output was told to stop before it had anything to stop
    assert events.ids(E.TTS_INTERRUPTED) == ["s1"]


async def test_an_interruption_with_nothing_to_interrupt_changes_nothing() -> None:
    pipeline, out, _, events = make()

    await pipeline.interrupt(reason="barge_in")
    out.release.set()
    await pipeline.say(sentence("s1"))

    assert out.stops == []  # the output was not touched
    assert out.played_ids() == ["s1"]
    assert events.ids(E.TTS_FINISHED) == ["s1"]


async def test_a_second_interruption_stops_what_the_first_one_let_through() -> None:
    pipeline, out, _, events = make()
    a = asyncio.create_task(pipeline.say(sentence("a1")))
    await out.playing.wait()
    a_next = asyncio.create_task(pipeline.say(sentence("a2")))
    await settle()
    await pipeline.interrupt(reason="barge_in")
    b_next = asyncio.create_task(pipeline.say(sentence("b1")))
    await settle()
    await pipeline.interrupt(reason="kill_switch")
    out.release.set()
    await asyncio.gather(a, a_next, b_next)

    assert out.played_ids() == ["a1", "b1"]  # b1 belonged to the new generation, and plays
    assert events.ids(E.TTS_INTERRUPTED) == ["a1", "a2", "b1"]  # until the second one cuts it
