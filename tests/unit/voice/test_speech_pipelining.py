"""Speech as one continuous stream: the next sentence is prepared while this one is heard.

Two kinds of test. The ordering ones hold an output device "playing" by an event and look at what
the engine was asked for in the meantime. The timing ones run the pipeline on a virtual clock in
which synthesis costs time and a device takes as long to play audio as the audio lasts, so the
silence between two sentences is a number the test can compare with the budget - with and without
the look-ahead - and nothing ever waits for real time.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import AsyncIterator
from typing import Any

import pytest

from nox.core.events import E
from nox.voice.base import Channel, TtsRequest
from nox.voice.latency import GAP_MIN_SAMPLES, SENTENCE_GAP_BUDGET_MS
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.tts.clips import ClipNotFoundError
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt, settle
from tests.unit.voice.test_interrupt_generations import Events

RATE = 24000


def sentence(name: str, **extra: Any) -> TtsRequest:
    return TtsRequest(utterance_id=name, text=f"Satz {name}", **extra)


class LabelTts:
    """An engine whose audio is its sentence's own id, in `chunks` pieces, and that keeps count of
    what it was asked for and how far each stream was pulled."""

    id = "label-tts"
    sample_rate = RATE

    def __init__(self, *, chunks: int = 3, chunk_bytes: int = 8) -> None:
        self.chunks = chunks
        self.chunk_bytes = chunk_bytes
        self.requests: list[TtsRequest] = []
        self.pulled: dict[str, int] = {}
        self.closed: list[str] = []
        self.fail_with: dict[str, Exception] = {}

    @property
    def asked(self) -> list[str]:
        return [r.utterance_id for r in self.requests]

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        uid = request.utterance_id
        self.requests.append(request)
        self.pulled[uid] = 0
        try:
            for _ in range(self.chunks):
                if uid in self.fail_with:
                    raise self.fail_with[uid]
                self.pulled[uid] += 1
                yield uid.encode().ljust(self.chunk_bytes, b".")
        finally:
            self.closed.append(uid)

    async def health(self) -> tuple[object, str]:  # pragma: no cover - unused by the pipeline
        raise NotImplementedError

    async def load(self) -> None:  # pragma: no cover
        return None

    async def unload(self) -> None:  # pragma: no cover
        return None


class HeldPlayback(FakeAudioOutput):
    """Takes every chunk at once, then stays "playing" until the test lets that sentence end."""

    def __init__(self) -> None:
        super().__init__()
        self.holds: dict[str, asyncio.Event] = {}
        self.playing: list[str] = []
        #: A device that does not end the playing sentence when told to stop.
        self.stubborn = False

    def hold(self, utterance_id: str) -> asyncio.Event:
        return self.holds.setdefault(utterance_id, asyncio.Event())

    def heard(self, utterance_id: str) -> list[bytes]:
        return next(chunks for uid, _, chunks in self.played if uid == utterance_id)

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
        self.playing.append(utterance_id)
        async for chunk in pcm_chunks:
            if self._stopped.is_set():
                return
            chunks.append(chunk)
        if utterance_id in self.holds:
            waiters = {asyncio.ensure_future(self.holds[utterance_id].wait())}
            if not self.stubborn:
                waiters.add(asyncio.ensure_future(self._stopped.wait()))
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in waiters:
                waiter.cancel()


def make(
    *,
    tts: LabelTts | None = None,
    allowed: Any = None,
    **config: Any,
) -> tuple[DefaultVoicePipeline, HeldPlayback, LabelTts, Events]:
    out, engine, events = HeldPlayback(), tts or LabelTts(), Events()
    pipeline = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=out,
        stt=FakeStt(),
        tts=engine,  # type: ignore[arg-type]
        emit=events,
        capture_allowed=lambda: True,
        config=PipelineConfig(
            edge_fade_ms=0, trim_silence=False, **config
        ),  # the fake engine's bytes are labels
        prefetch_allowed=allowed or (lambda: True),
    )
    return pipeline, out, engine, events


def start_say(pipeline: DefaultVoicePipeline, *names: str) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(pipeline.say(sentence(n))) for n in names]


# ---- what is prepared when -------------------------------------------------------------------


async def test_the_next_sentence_is_synthesised_while_the_first_is_still_heard() -> None:
    pipeline, out, tts, _ = make()
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2")
    await settle()

    assert out.playing == ["s1"]  # s2 has not been heard ...
    assert tts.asked == ["s1", "s2"]  # ... but it has been synthesised

    out.holds["s1"].set()
    await asyncio.gather(*tasks)
    assert out.heard("s2") == [b"s2......"] * 3  # and it played from what was prepared


async def test_without_the_look_ahead_a_sentence_is_synthesised_when_its_turn_comes() -> None:
    pipeline, out, tts, _ = make(prefetch_sentences=0)
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2")
    await settle()

    assert tts.asked == ["s1"]

    out.holds["s1"].set()
    await asyncio.gather(*tasks)
    assert tts.asked == ["s1", "s2"] and out.heard("s2") == [b"s2......"] * 3


async def test_the_look_ahead_is_as_far_as_configured_and_no_further() -> None:
    pipeline, out, tts, _ = make(prefetch_sentences=1)
    out.hold("s1")
    out.hold("s2")
    tasks = start_say(pipeline, "s1", "s2", "s3", "s4")
    await settle()
    assert tts.asked == ["s1", "s2"]  # one ahead

    out.holds["s1"].set()  # s1 ends, s2 starts playing: now s3 is the one ahead
    await settle()
    assert tts.asked == ["s1", "s2", "s3"]

    out.holds["s2"].set()
    await asyncio.gather(*tasks)
    assert tts.asked == ["s1", "s2", "s3", "s4"]


async def test_two_sentences_ahead() -> None:
    pipeline, out, tts, _ = make(prefetch_sentences=2)
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2", "s3", "s4")
    await settle()

    assert tts.asked == ["s1", "s2", "s3"]

    out.holds["s1"].set()
    await asyncio.gather(*tasks)


async def test_a_sentence_requested_while_another_plays_is_prepared_at_once() -> None:
    pipeline, out, tts, _ = make()
    out.hold("s1")
    first = start_say(pipeline, "s1")
    await settle()
    assert tts.asked == ["s1"]

    second = start_say(pipeline, "s2")  # the model produced the next sentence meanwhile
    await settle()

    assert tts.asked == ["s1", "s2"]
    out.holds["s1"].set()
    await asyncio.gather(*first, *second)


async def test_a_busy_machine_gets_no_look_ahead_the_sentence_waits_for_its_turn() -> None:
    pipeline, out, tts, _ = make(allowed=lambda: False)
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2")
    await settle()

    assert tts.asked == ["s1"]  # the one that plays is synthesised in any case

    out.holds["s1"].set()
    await asyncio.gather(*tasks)
    assert tts.asked == ["s1", "s2"] and out.heard("s2") == [b"s2......"] * 3


async def test_the_look_ahead_catches_up_when_the_machine_has_room_again() -> None:
    room = {"free": False}
    pipeline, out, tts, _ = make(allowed=lambda: room["free"])
    out.hold("s1")
    first = start_say(pipeline, "s1", "s2")
    await settle()
    assert tts.asked == ["s1"]

    room["free"] = True  # the game was closed
    later = start_say(pipeline, "s3")  # the next sentence arrives and the plan is made again
    await settle()

    assert tts.asked == ["s1", "s2"]  # s2 is the one next in line, so s2 is the one prepared
    out.holds["s1"].set()
    await asyncio.gather(*first, *later)


async def test_the_buffer_of_a_sentence_has_a_limit_so_the_engine_cannot_run_away() -> None:
    # 0.002 s of 24 kHz PCM16 is 96 bytes: three 32-byte chunks.
    tts = LabelTts(chunks=200, chunk_bytes=32)
    pipeline, out, _, _ = make(tts=tts, prefetch_buffer_s=0.002)
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2")
    await settle(50)

    assert tts.pulled["s2"] <= 4  # a few chunks, not the 200 the engine has

    out.holds["s1"].set()
    await asyncio.gather(*tasks)
    assert len(out.heard("s2")) == 200  # all of it, in the end, once it is being heard


# ---- interrupting -----------------------------------------------------------------------------


async def test_an_interruption_throws_the_prepared_audio_away_and_stops_the_engine() -> None:
    tts = LabelTts(chunks=200, chunk_bytes=32)
    pipeline, out, _, events = make(tts=tts, prefetch_sentences=2, prefetch_buffer_s=0.002)
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2", "s3")
    await settle(50)
    assert tts.asked == ["s1", "s2", "s3"]  # two sentences are ready, one of them part-way

    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(*tasks)

    assert sorted(tts.closed) == ["s1", "s2", "s3"]  # every engine stream was closed
    assert out.playing == ["s1"]  # nothing of s2 or s3 reached the device
    assert events.ids(E.TTS_INTERRUPTED) == ["s1", "s2", "s3"]
    assert events.of(E.TTS_FINISHED) == []
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("voice-prefetch")]


async def test_the_look_ahead_is_cancelled_by_the_interruption_not_when_its_turn_would_come() -> (
    None
):
    """A device that takes its time to stop keeps the speech lock for a while. The sentences
    waiting behind it are not dropped until it lets go - but the synthesis that was running for
    them must stop at once, whatever the device does."""
    tts = LabelTts(chunks=200, chunk_bytes=32)  # streams that are still open when the cut comes
    pipeline, out, _, _ = make(tts=tts, prefetch_sentences=2, prefetch_buffer_s=0.002)
    out.hold("s1")
    out.stubborn = True  # `stop` does not end the playing sentence; only the test's release does
    tasks = start_say(pipeline, "s1", "s2", "s3")
    await settle()
    assert tts.asked == ["s1", "s2", "s3"]

    assert tts.closed == []  # all three are mid-sentence: nothing has ended by itself
    await pipeline.interrupt(reason="barge_in")
    await settle()

    assert sorted(tts.closed) == ["s1", "s2", "s3"]  # all closed, s1 still holds the device
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("voice-prefetch")]
    out.holds["s1"].set()
    await asyncio.gather(*tasks)


async def test_a_sentence_of_the_next_turn_is_never_given_audio_prepared_for_the_last() -> None:
    pipeline, out, tts, events = make(prefetch_sentences=2)
    out.hold("old-1")
    old = start_say(pipeline, "old-1", "old-2")
    await settle()
    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(*old)

    await pipeline.say(sentence("new-1"))

    assert out.heard("new-1") == [b"new-1..."] * 3
    assert "old-2" not in [uid for uid, _, _ in out.played]
    assert events.ids(E.TTS_FINISHED) == ["new-1"]


async def test_a_sentence_requested_after_an_interruption_is_not_dropped_and_is_prepared() -> None:
    pipeline, out, tts, _ = make()
    out.hold("old-1")
    old = start_say(pipeline, "old-1")
    await settle()
    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(*old)
    out.hold("new-1")
    new = start_say(pipeline, "new-1", "new-2")
    await settle()

    assert tts.asked == ["old-1", "new-1", "new-2"]  # the look-ahead works in the new generation

    out.holds["new-1"].set()
    await asyncio.gather(*new)


async def test_stopping_the_pipeline_cancels_what_was_prepared() -> None:
    pipeline, out, tts, events = make(prefetch_sentences=2)
    await pipeline.start()
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2", "s3")
    await settle()

    await pipeline.stop()
    await asyncio.gather(*tasks)

    assert sorted(tts.closed) == ["s1", "s2", "s3"]  # none of the three is left synthesising
    assert events.ids(E.TTS_INTERRUPTED) == ["s1"]  # the one that was heard; the others just end
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("voice-prefetch")]


# ---- the engine's failures --------------------------------------------------------------------


async def test_a_prepared_sentence_that_fails_is_reported_and_the_next_one_still_plays() -> None:
    pipeline, out, tts, events = make()
    tts.fail_with["s2"] = ValueError("no such voice")
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2", "s3")
    await settle()
    out.holds["s1"].set()
    await asyncio.gather(*tasks)

    finished = {p["utterance_id"]: p for p in events.of(E.TTS_FINISHED)}
    assert finished["s2"] == {"utterance_id": "s2", "ok": False, "reason": "ValueError"}
    assert finished["s1"]["ok"] and finished["s3"]["ok"]
    assert out.heard("s3") == [b"s3......"] * 3


async def test_an_unexpected_engine_error_is_reported_then_raised_to_the_caller() -> None:
    pipeline, out, tts, events = make()
    tts.fail_with["s1"] = RuntimeError("engine crashed")

    with pytest.raises(RuntimeError, match="engine crashed"):
        await pipeline.say(sentence("s1"))

    assert events.of(E.TTS_FINISHED) == [
        {"utterance_id": "s1", "ok": False, "reason": "RuntimeError"}
    ]


class ClipEngine(LabelTts):
    """A callout names a clip. `missing`: no clip at all; `broken`: one chunk, then it is gone."""

    def __init__(self) -> None:
        super().__init__()
        self.missing: set[str] = set()
        self.broken: set[str] = set()

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        uid = request.utterance_id
        self.requests.append(request)
        try:
            if request.prepared_clip in self.missing:
                raise ClipNotFoundError(request.prepared_clip)
            if request.prepared_clip in self.broken:
                yield b"clip-start"
                raise ClipNotFoundError(request.prepared_clip)
            yield f"spoken:{request.text}".encode()
        finally:
            self.closed.append(uid)


async def test_a_missing_clip_is_spoken_from_its_words_when_it_was_prepared_ahead() -> None:
    engine = ClipEngine()
    engine.missing.add("careful")
    pipeline, out, _, events = make(tts=engine)
    out.hold("s1")
    callout = TtsRequest(utterance_id="c1", text="Careful.", prepared_clip="careful")
    first = start_say(pipeline, "s1")
    second = asyncio.create_task(pipeline.say(callout))
    await settle()
    assert [r.prepared_clip for r in engine.requests] == [None, "careful", None]  # tried, fell back

    out.holds["s1"].set()
    await asyncio.gather(*first, second)

    assert out.heard("c1") == [b"spoken:Careful."]
    assert {"utterance_id": "c1", "ok": True} in events.of(E.TTS_FINISHED)


async def test_a_clip_that_fails_after_its_first_chunk_is_not_papered_over() -> None:
    engine = ClipEngine()
    engine.broken.add("careful")
    pipeline, out, _, events = make(tts=engine)
    out.hold("s1")
    callout = TtsRequest(utterance_id="c1", text="Careful.", prepared_clip="careful")
    first = start_say(pipeline, "s1")
    second = asyncio.create_task(pipeline.say(callout))
    await settle()
    out.holds["s1"].set()
    await asyncio.gather(*first, second)

    assert [r.prepared_clip for r in engine.requests] == [None, "careful"]  # no second attempt
    assert out.heard("c1") == [b"clip-start"]  # what was said before the failure was said
    assert {"utterance_id": "c1", "ok": False, "reason": "ClipNotFoundError"} in events.of(
        E.TTS_FINISHED
    )


# ---- the silence between sentences, on a virtual clock -----------------------------------------


class VirtualClock:
    """Time that moves only when every task is waiting for it: sleeping costs nothing real."""

    def __init__(self) -> None:
        self.now = 0.0
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._count = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + seconds, self._count, future))
        self._count += 1
        await future

    async def run(self, *awaitables: Any) -> None:
        tasks = [asyncio.ensure_future(a) for a in awaitables]
        while not all(t.done() for t in tasks):
            await settle(60)
            if all(t.done() for t in tasks):
                break
            while self._timers and self._timers[0][2].cancelled():
                heapq.heappop(self._timers)
            if not self._timers:
                raise AssertionError("every task is waiting and no timer is running")
            when, _, future = heapq.heappop(self._timers)
            self.now = max(self.now, when)
            future.set_result(None)
        for t in tasks:
            t.result()


class SimTts:
    """Synthesis takes `synth_s` of the clock before the first audio, the audio lasts `audio_s`."""

    id = "sim-tts"
    sample_rate = RATE

    def __init__(self, clock: VirtualClock, *, synth_s: float, audio_s: float) -> None:
        self.clock = clock
        self.synth_s = synth_s
        self.audio_s = audio_s

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        await self.clock.sleep(self.synth_s)
        for _ in range(4):
            yield b"\x00\x00" * int(RATE * self.audio_s / 4)

    async def health(self) -> tuple[object, str]:  # pragma: no cover
        raise NotImplementedError

    async def load(self) -> None:  # pragma: no cover
        return None

    async def unload(self) -> None:  # pragma: no cover
        return None


class SimDevice(FakeAudioOutput):
    """A device that plays audio as it arrives and takes as long as the audio lasts."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self.clock = clock

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        free_at = self.clock.now
        async for chunk in pcm_chunks:
            free_at = max(free_at, self.clock.now) + len(chunk) / (2 * sample_rate)
        await self.clock.sleep(free_at - self.clock.now)


def timed(clock: VirtualClock, *, synth_s: float, audio_s: float, **config: Any) -> Any:
    return DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=SimDevice(clock),
        stt=FakeStt(),
        tts=SimTts(clock, synth_s=synth_s, audio_s=audio_s),  # type: ignore[arg-type]
        emit=Events(),
        capture_allowed=lambda: True,
        config=PipelineConfig(edge_fade_ms=0, trim_silence=False, **config),
        clock=clock,
        sleep=clock.sleep,
    )


async def speak_answer(clock: VirtualClock, pipeline: Any, sentences: int) -> None:
    """A model that has produced `sentences` sentences by the time the first one is heard."""
    await clock.run(*[pipeline.say(sentence(f"s{i}")) for i in range(sentences)])


async def test_with_the_look_ahead_the_sentences_follow_each_other_without_a_pause() -> None:
    clock = VirtualClock()
    pipeline = timed(clock, synth_s=0.4, audio_s=2.0)

    await speak_answer(clock, pipeline, 6)

    summary = pipeline.latency_summary()
    assert summary.sentence_gaps == 5  # between six sentences
    assert summary.sentence_gap_ms is not None and summary.sentence_gap_ms.p95 == 0.0
    assert summary.sentence_gap_ok is True
    assert summary.sentence_gap_budget_ms == SENTENCE_GAP_BUDGET_MS == 120.0


async def test_without_it_every_boundary_costs_the_engines_whole_start_up_time() -> None:
    clock = VirtualClock()
    pipeline = timed(clock, synth_s=0.4, audio_s=2.0, prefetch_sentences=0)

    await speak_answer(clock, pipeline, 6)

    summary = pipeline.latency_summary()
    assert summary.sentence_gap_ms is not None
    assert summary.sentence_gap_ms.p50 == summary.sentence_gap_ms.p95 == 400.0
    assert summary.sentence_gap_ok is False  # over the 120 ms budget: this is what was heard


async def test_the_budget_holds_when_the_engine_is_slower_than_the_sentence_is_short() -> None:
    """Synthesis that takes longer than the sentence lasts cannot be hidden by a one-sentence
    look-ahead: the gap that is left is the part the look-ahead could not cover, and it is
    measured, not guessed."""
    clock = VirtualClock()
    pipeline = timed(clock, synth_s=1.5, audio_s=1.0)

    await speak_answer(clock, pipeline, 4)

    gaps = pipeline.latency_summary().sentence_gap_ms
    assert gaps is not None and 0.0 < gaps.p95 < 1500.0


async def test_a_sentence_that_opens_an_answer_has_no_gap_to_measure() -> None:
    clock = VirtualClock()
    pipeline = timed(clock, synth_s=0.4, audio_s=1.0)

    await clock.run(pipeline.say(sentence("a1")))
    await clock.run(pipeline.say(sentence("b1")))  # a new answer, long after the first

    summary = pipeline.latency_summary()
    assert summary.sentence_gaps == 0 and summary.sentence_gap_ms is None
    assert summary.sentence_gap_ok is None


async def test_an_interrupted_answer_leaves_no_gap_behind() -> None:
    pipeline, out, _, _ = make()
    out.hold("s1")
    tasks = start_say(pipeline, "s1", "s2")
    await settle()
    await pipeline.interrupt(reason="barge_in")
    await asyncio.gather(*tasks)

    await pipeline.say(sentence("n1"))

    assert pipeline.latency_summary().sentence_gaps == 0


def test_health_waits_for_enough_gaps_before_it_judges() -> None:
    assert GAP_MIN_SAMPLES == 8
