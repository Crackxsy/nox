"""Silence around a sentence is cut, and the first-sound number is a number the listener hears.

An engine puts about half a second of silence in front of a sentence and more behind it. The
first part measures the cut on synthetic audio; the second runs the pipeline on a virtual clock
with an output that knows when the first audible sample plays, and holds the reported time to the
first sound to that instant - with the cut, and (as the contrast) without it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from nox.voice.base import Channel, TtsRequest
from nox.voice.edges import trim_silence
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from tests.unit.voice.conftest import FakeAudioInput, FakeAudioOutput, FakeStt
from tests.unit.voice.test_interrupt_generations import Events
from tests.unit.voice.test_speech_pipelining import RATE, VirtualClock

LEVEL = 8000


def pcm(samples: np.ndarray) -> bytes:
    return samples.astype("<i2").tobytes()


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def speech(seconds: float, level: int = LEVEL) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (level * np.cos(2 * np.pi * 220 * t)).astype(np.int16)


async def source(data: bytes, size: int) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def trimmed(audio: np.ndarray, *, size: int = 4800, **kwargs: Any) -> np.ndarray:
    out = b"".join([c async for c in trim_silence(source(pcm(audio), size), RATE, **kwargs)])
    return np.frombuffer(out, dtype="<i2")


async def test_the_silence_before_and_after_the_speech_is_cut() -> None:
    audio = np.concatenate([silence(0.5), speech(1.0), silence(0.65)])

    out = await trimmed(audio)

    lead, tail = int(RATE * 0.010), int(RATE * 0.060)
    assert out.size == lead + RATE + tail  # 10 ms in front, the speech, 60 ms behind
    assert np.array_equal(out[lead : lead + RATE], speech(1.0))  # the speech itself is untouched
    assert audio.size - out.size > RATE * 1.0  # more than a second of dead air gone


async def test_a_pause_inside_the_sentence_is_kept_whole() -> None:
    audio = np.concatenate([silence(0.5), speech(0.4), silence(0.3), speech(0.4), silence(0.6)])

    out = await trimmed(audio)

    lead, tail = int(RATE * 0.010), int(RATE * 0.060)
    assert out.size == lead + int(RATE * 1.1) + tail
    middle = out[lead + int(RATE * 0.4) : lead + int(RATE * 0.7)]
    assert not middle.any()  # the 0.3 s pause is still all there


@pytest.mark.parametrize("size", [2, 100, 4801, 10_000, 1_000_000])
async def test_the_result_does_not_depend_on_how_the_engine_chunked_the_audio(size: int) -> None:
    audio = np.concatenate([silence(0.5), speech(0.3), silence(0.2), speech(0.3), silence(0.6)])

    assert np.array_equal(await trimmed(audio, size=size), await trimmed(audio, size=4800))


async def test_a_sentence_that_is_all_silence_gives_nothing() -> None:
    assert (await trimmed(silence(1.0))).size == 0


async def test_speech_right_at_the_start_and_end_loses_nothing() -> None:
    audio = speech(0.5)

    assert np.array_equal(await trimmed(audio), audio)


async def test_the_noise_floor_of_a_synthesis_model_counts_as_silence() -> None:
    rng = np.random.default_rng(3)
    floor = (rng.standard_normal(RATE // 2) * 20).astype(np.int16)  # about -64 dBFS
    audio = np.concatenate([floor, speech(0.5), floor])

    out = await trimmed(audio)

    assert out.size < audio.size - int(RATE * 0.8)


async def test_a_quiet_voice_is_not_cut_when_the_threshold_is_lowered() -> None:
    audio = np.concatenate([silence(0.3), speech(0.5, level=60), silence(0.3)])  # about -55 dBFS

    default = await trimmed(audio)
    careful = await trimmed(audio, threshold_db=-70.0)

    assert default.size == 0  # taken for silence at the default threshold
    assert careful.size > int(RATE * 0.5)


async def test_no_silence_to_keep_when_asked_for_none() -> None:
    audio = np.concatenate([silence(0.5), speech(0.4), silence(0.5)])

    out = await trimmed(audio, lead_ms=0.0, tail_ms=0.0)

    assert np.array_equal(out, speech(0.4))


async def test_a_stray_odd_byte_is_dropped_not_misread() -> None:
    audio = pcm(np.concatenate([silence(0.2), speech(0.2)])) + b"\x01"

    out = b"".join([c async for c in trim_silence(source(audio, 999), RATE)])

    assert len(out) % 2 == 0 and len(out) > 0


async def test_an_empty_stream_gives_nothing() -> None:
    assert [c async for c in trim_silence(source(b"", 10), RATE)] == []


# ---- the number the listener lives by ----------------------------------------------------------


class AudibleDevice(FakeAudioOutput):
    """Plays what it is given and notes when the first sample above the threshold is heard."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self.clock = clock
        self.first_audible_at: float | None = None

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
            samples = np.frombuffer(chunk, dtype="<i2")
            begins = max(free_at, self.clock.now)
            loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > 104)
            if loud.size and self.first_audible_at is None:
                self.first_audible_at = begins + float(loud[0]) / sample_rate
            free_at = begins + samples.size / sample_rate
        await self.clock.sleep(free_at - self.clock.now)


class PaddedTts:
    """Takes `synth_s` to start, then says 0.5 s of silence, a second of speech, 0.65 s of silence
    - what the default engine does."""

    id = "padded-tts"
    sample_rate = RATE

    def __init__(self, clock: VirtualClock, synth_s: float) -> None:
        self.clock = clock
        self.synth_s = synth_s

    async def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        await self.clock.sleep(self.synth_s)
        yield pcm(np.concatenate([silence(0.5), speech(1.0), silence(0.65)]))

    async def health(self) -> tuple[object, str]:  # pragma: no cover
        raise NotImplementedError

    async def load(self) -> None:  # pragma: no cover
        return None

    async def unload(self) -> None:  # pragma: no cover
        return None


async def first_sound(trim: bool) -> tuple[float, float]:
    """(reported ms from the end of the utterance to the first sound, ms until it is heard)."""
    clock = VirtualClock()
    device = AudibleDevice(clock)
    pipeline = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=device,
        stt=FakeStt(),
        tts=PaddedTts(clock, synth_s=0.3),  # type: ignore[arg-type]
        emit=Events(),
        capture_allowed=lambda: True,
        config=PipelineConfig(edge_fade_ms=0, trim_silence=trim),
        clock=clock,
        sleep=clock.sleep,
    )
    pipeline._latency.begin(  # noqa: SLF001 - an addressed transcript has just been published
        ended_at=0.0, transcribed_at=0.0, published_at=0.0, endpoint_ms=0.0
    )
    await clock.run(pipeline.say(TtsRequest(utterance_id="s1", text="Hallo.")))

    summary = pipeline.latency_summary()
    assert summary.total_ms is not None and device.first_audible_at is not None
    return summary.total_ms.p50, device.first_audible_at * 1000.0


async def test_the_reported_time_to_the_first_sound_is_when_the_sound_is_heard() -> None:
    reported, heard = await first_sound(trim=True)

    assert reported == pytest.approx(300.0)  # the engine's start-up time and nothing else
    assert abs(heard - reported) <= 15.0  # the 10 ms of lead that is kept, not half a second


async def test_without_the_cut_the_number_was_half_a_second_too_good() -> None:
    reported, heard = await first_sound(trim=False)

    assert reported == pytest.approx(300.0)
    assert heard - reported == pytest.approx(500.0)  # the silence the listener sat through


async def test_the_silence_between_sentences_is_gone_with_the_cut() -> None:
    """Two sentences, each padded like the engine pads them: heard back to back."""
    clock = VirtualClock()
    device = AudibleDevice(clock)
    pipeline = DefaultVoicePipeline(
        audio_in=FakeAudioInput(),
        audio_out=device,
        stt=FakeStt(),
        tts=PaddedTts(clock, synth_s=0.3),  # type: ignore[arg-type]
        emit=Events(),
        capture_allowed=lambda: True,
        config=PipelineConfig(edge_fade_ms=0),
        clock=clock,
        sleep=clock.sleep,
    )

    started = clock.now
    await clock.run(
        pipeline.say(TtsRequest(utterance_id="s1", text="Eins.")),
        pipeline.say(TtsRequest(utterance_id="s2", text="Zwei.")),
    )

    # first sound at 0.31 s, two sentences of 1.07 s: over at about 2.45 s. Without the cut
    # there would be 1.15 s of silence more per sentence.
    assert clock.now - started < 2.6
    assert pipeline.latency_summary().sentence_gap_ok is True


async def test_the_cut_can_be_switched_off() -> None:
    audio_in = FakeAudioInput()
    out = FakeAudioOutput()
    clock = VirtualClock()
    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=out,
        stt=FakeStt(),
        tts=PaddedTts(clock, synth_s=0.0),  # type: ignore[arg-type]
        emit=Events(),
        capture_allowed=lambda: True,
        config=PipelineConfig(edge_fade_ms=0, trim_silence=False),
        clock=clock,
        sleep=clock.sleep,
    )

    await asyncio.wait_for(clock.run(pipeline.say(TtsRequest(utterance_id="s1", text="x."))), 5)

    played = b"".join(out.played[0][2])
    assert len(played) == 2 * int(RATE * 2.15)
