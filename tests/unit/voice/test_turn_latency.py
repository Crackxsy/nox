"""The time from the end of an utterance to the first sound back, measured on a fake clock.

`TurnTimings` covers the core's part of a turn; these tests cover what the user feels: the
end-of-speech decision, transcription, the wait for the first sentence, and the speech synthesis in
front of the first audio chunk. Every number is computed from a clock the test moves by hand.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from nox.core.events import E
from nox.voice import pipeline as pipeline_module
from nox.voice.base import Transcript, TtsRequest
from nox.voice.latency import LatencyTracker, percentile
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.vad import Segmenter
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeStt,
    FakeTts,
    frames_of,
    noise,
    settle,
    tone,
)


class RecordingLog:
    """Stands in for the pipeline's logger and keeps every call as (event, fields)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **fields: Any) -> None:
        self.entries.append((event, fields))

    info = debug = warning = error = _record


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


# ---- the tracker ------------------------------------------------------------------------------


def one_turn(
    tracker: LatencyTracker, clock: Clock, *, stt: float, publish: float, reply: float, synth: float
) -> None:
    """Run one turn through the tracker, moving the clock by the given milliseconds per stage."""
    ended = clock.now
    clock.advance_ms(stt)
    transcribed = clock.now
    clock.advance_ms(publish)
    tracker.begin(
        ended_at=ended, transcribed_at=transcribed, published_at=clock.now, endpoint_ms=600.0
    )
    clock.advance_ms(reply)
    ticket = tracker.say_requested()
    assert ticket is not None
    clock.advance_ms(synth)
    tracker.first_audio(ticket)


def test_a_turn_is_split_into_its_stages_and_summed_to_a_total() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock)
    one_turn(tracker, clock, stt=140, publish=3, reply=700, synth=90)

    summary = tracker.summary()
    assert summary.last == {
        "stt_ms": 140.0,
        "publish_ms": 3.0,
        "reply_ms": 700.0,
        "synth_ms": 90.0,
        "total_ms": 933.0,
        "endpoint_ms": 600.0,
    }
    assert summary.turns == 1 and summary.abandoned == 0


def test_percentiles_use_the_nearest_rank_over_the_recorded_turns() -> None:
    assert percentile([100.0 * i for i in range(1, 11)], 50) == 500.0
    assert percentile([100.0 * i for i in range(1, 11)], 95) == 1000.0
    assert percentile([float(i) for i in range(1, 21)], 50) == 10.0
    assert percentile([float(i) for i in range(1, 21)], 95) == 19.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


def test_the_summary_reports_p50_and_p95_of_the_totals() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock)
    for total in range(100, 2100, 100):  # twenty turns: 100 ms ... 2000 ms
        one_turn(tracker, clock, stt=total, publish=0, reply=0, synth=0)

    spread = tracker.summary().total_ms
    assert spread is not None
    assert (spread.p50, spread.p95) == (1000.0, 1900.0)


def test_only_the_most_recent_turns_count() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock, window=3)
    for total in (9000, 9000, 100, 200, 300):
        one_turn(tracker, clock, stt=total, publish=0, reply=0, synth=0)

    summary = tracker.summary()
    assert (summary.turns, summary.window) == (3, 3)
    assert summary.total_ms is not None and summary.total_ms.p95 == 300.0


def test_an_empty_tracker_has_nothing_to_report_rather_than_zeros() -> None:
    summary = LatencyTracker(clock=Clock()).summary()
    assert summary.turns == 0
    assert summary.total_ms is None and summary.last is None


def test_only_the_first_sentence_of_a_turn_is_timed() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin(ended_at=0.0, transcribed_at=0.1, published_at=0.1, endpoint_ms=0.0)

    assert tracker.say_requested() is not None
    assert tracker.say_requested() is None  # the second sentence is not the first sound


def test_speech_without_an_open_turn_is_not_timed() -> None:
    """A proactive remark or a chat answer spoken aloud was not preceded by the user's voice."""
    tracker = LatencyTracker(clock=Clock())
    assert tracker.say_requested() is None


def test_a_newer_turn_replaces_an_unfinished_one_and_the_old_ticket_reports_nothing() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin(ended_at=0.0, transcribed_at=0.1, published_at=0.1, endpoint_ms=0.0)
    stale = tracker.say_requested()
    assert stale is not None

    tracker.begin(ended_at=5.0, transcribed_at=5.1, published_at=5.1, endpoint_ms=0.0)
    current = tracker.say_requested()
    assert current is not None
    clock.advance_ms(6000)

    assert tracker.first_audio(stale) is None  # the old turn's sound is not the new turn's
    assert tracker.first_audio(current) is not None  # and it did not use up the new turn
    assert tracker.abandoned == 1
    assert tracker.summary().turns == 1


def test_an_answer_that_starts_far_too_late_is_not_counted_as_the_turns_latency() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock, reply_timeout_s=60.0)
    tracker.begin(ended_at=0.0, transcribed_at=0.1, published_at=0.1, endpoint_ms=0.0)
    clock.now = 61.0

    assert tracker.say_requested() is None
    assert tracker.abandoned == 1


def test_a_clock_that_steps_back_never_produces_a_negative_stage() -> None:
    clock = Clock()
    tracker = LatencyTracker(clock=clock)
    tracker.begin(ended_at=10.0, transcribed_at=9.0, published_at=9.5, endpoint_ms=0.0)
    ticket = tracker.say_requested()
    assert ticket is not None
    result = tracker.first_audio(ticket)
    assert result is not None
    assert min(result.stt_ms, result.publish_ms, result.reply_ms, result.synth_ms) >= 0.0


# ---- through the pipeline ---------------------------------------------------------------------


class SlowStt(FakeStt):
    def __init__(self, clock: Clock, *, text: str, ms: float) -> None:
        super().__init__(text)
        self._clock = clock
        self._ms = ms

    async def transcribe(
        self, audio: np.ndarray, sample_rate: int, *, language: str = "auto"
    ) -> Transcript:
        self._clock.advance_ms(self._ms)
        return await super().transcribe(audio, sample_rate, language=language)


class SlowTts(FakeTts):
    """Synthesis takes `ms` before its first chunk."""

    def __init__(self, clock: Clock, *, ms: float) -> None:
        super().__init__()
        self._clock = clock
        self._ms = ms

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        self._clock.advance_ms(self._ms)
        async for chunk in super().synthesize(request):
            yield chunk


class Rig:
    def __init__(self, *, text: str = "Nox wie spät ist es", **config: Any) -> None:
        self.clock = Clock()
        self.audio_in = FakeAudioInput()
        self.published: list[str] = []

        async def emit(name: str, _payload: dict[str, Any]) -> None:
            if name == E.VOICE_TRANSCRIPT_READY:
                self.clock.advance_ms(3)  # the send to the core
                self.published.append(name)

        self.pipeline = DefaultVoicePipeline(
            audio_in=self.audio_in,
            audio_out=FakeAudioOutput(),
            stt=SlowStt(self.clock, text=text, ms=140),
            tts=SlowTts(self.clock, ms=90),
            emit=emit,
            capture_allowed=lambda: True,
            config=PipelineConfig(end_silence_ms=300, **config),
            segmenter=Segmenter(
                sample_rate=16000, end_silence_ms=300, min_segment_ms=200, pre_roll_ms=60
            ),
            clock=self.clock,
        )

    async def speak_the_utterance(self) -> None:
        self.audio_in.push_all(frames_of(np.concatenate([noise(300), tone(600), noise(500)])))
        for _ in range(200):
            await asyncio.sleep(0)
            if self.published:
                return
        raise AssertionError("the utterance never became a transcript")


async def test_a_voice_turn_is_measured_from_the_utterance_end_to_the_first_audio_chunk() -> None:
    rig = Rig()
    await rig.pipeline.start()
    await rig.speak_the_utterance()

    rig.clock.advance_ms(700)  # the core: context, model, first sentence
    await rig.pipeline.say(TtsRequest(utterance_id="u1", text="Es ist zehn Uhr."))

    summary = rig.pipeline.latency_summary()
    assert summary.last == {
        "stt_ms": 140.0,
        "publish_ms": 3.0,
        "reply_ms": 700.0,
        "synth_ms": 90.0,
        "total_ms": 933.0,
        "endpoint_ms": 300.0,  # the configured silence the end-of-speech detector waits for
    }
    await rig.pipeline.stop()


async def test_a_push_to_talk_turn_has_no_end_of_speech_wait() -> None:
    rig = Rig()
    await rig.pipeline.start()
    await rig.pipeline.push_to_talk(True)
    rig.audio_in.push_all(frames_of(np.concatenate([tone(600)])))
    await settle()
    await rig.pipeline.push_to_talk(False)
    for _ in range(200):
        await asyncio.sleep(0)
        if rig.published:
            break

    await rig.pipeline.say(TtsRequest(utterance_id="u1", text="Ja."))

    last = rig.pipeline.latency_summary().last
    assert last is not None and last["endpoint_ms"] == 0.0
    await rig.pipeline.stop()


async def test_only_the_first_sentence_of_an_answer_is_timed() -> None:
    rig = Rig()
    await rig.pipeline.start()
    await rig.speak_the_utterance()

    await rig.pipeline.say(TtsRequest(utterance_id="u1", text="Erster Satz."))
    rig.clock.advance_ms(5000)
    await rig.pipeline.say(TtsRequest(utterance_id="u2", text="Zweiter Satz."))

    summary = rig.pipeline.latency_summary()
    assert summary.turns == 1
    assert summary.total_ms is not None and summary.total_ms.p95 < 1000.0
    await rig.pipeline.stop()


async def test_speech_that_nobody_asked_for_is_not_a_turn() -> None:
    rig = Rig()
    await rig.pipeline.start()

    await rig.pipeline.say(TtsRequest(utterance_id="p1", text="Du hast Post."))

    assert rig.pipeline.latency_summary().turns == 0
    await rig.pipeline.stop()


async def test_speech_that_was_not_addressed_to_nox_does_not_open_a_turn() -> None:
    rig = Rig(text="Das war ein guter Save")
    await rig.pipeline.start()
    await rig.speak_the_utterance()

    await rig.pipeline.say(TtsRequest(utterance_id="p1", text="Du hast Post."))

    assert rig.pipeline.latency_summary().turns == 0
    await rig.pipeline.stop()


async def test_each_measured_turn_is_logged_with_numbers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pipeline's logger is replaced, not captured: whatever logging configuration an
    # earlier test left behind decides what a captured logger sees, but not what this one gets.
    recorder = RecordingLog()
    monkeypatch.setattr(pipeline_module, "log", recorder)
    rig = Rig()
    await rig.pipeline.start()
    await rig.speak_the_utterance()

    await rig.pipeline.say(TtsRequest(utterance_id="u1", text="Es ist zehn Uhr."))

    entries = [fields for event, fields in recorder.entries if event == "voice.turn_latency"]
    assert len(entries) == 1
    assert entries[0]["total_ms"] == 233.0  # 140 transcribe + 3 publish + 90 synthesis
    assert "Uhr" not in str(entries[0])  # milliseconds only, never the words
    await rig.pipeline.stop()
