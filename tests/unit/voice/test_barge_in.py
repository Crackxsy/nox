"""Talking over Nox: a detector trigger alone does not cut an answer off, loud speech does.

The first half drives the gate with frames; the second drives a whole pipeline with a real
segmenter, so what is pinned is the behaviour: a cough or room noise leaves the answer playing,
a sentence spoken at the microphone stops it, a key press stops it at once.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from pydantic import ValidationError

from nox.core.events import E
from nox.voice.barge_in import DEFAULT_MIN_DB, DEFAULT_MIN_MS, BargeInGate
from nox.voice.base import TtsRequest
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.vad import Segmenter
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeStt,
    FakeTts,
    frames_of,
    noise,
    settle,
    tone,
)
from tests.unit.voice.test_interrupt_generations import Events, HeldOutput

LOUD = 0.3  # about -13 dBFS: a voice at the microphone
QUIET = 0.01  # about -43 dBFS: speech-like to the detector, far below a voice at the microphone


def frame(amp: float) -> np.ndarray:
    return tone(30, amp=amp)[:480]


# ---- the gate ---------------------------------------------------------------------------------


def test_the_defaults_are_the_documented_ones() -> None:
    assert (DEFAULT_MIN_MS, DEFAULT_MIN_DB) == (180, -38.0)
    config = PipelineConfig()
    assert (config.barge_in_min_ms, config.barge_in_min_db) == (180, -38.0)


def test_loud_frames_add_up_to_the_minimum_and_it_fires_once() -> None:
    gate = BargeInGate()
    gate.arm()

    fired = [gate.observe(frame(LOUD)) for _ in range(8)]

    assert fired == [False] * 5 + [True] + [False] * 2  # 6 frames of 30 ms = 180 ms
    assert gate.armed is False  # it fired and is done


def test_quiet_frames_never_add_up() -> None:
    gate = BargeInGate()
    gate.arm()

    assert not any(gate.observe(frame(QUIET)) for _ in range(300))
    assert gate.armed and gate.loud_ms == 0  # still waiting for something that is not noise


def test_a_level_exactly_at_the_threshold_counts() -> None:
    gate = BargeInGate(min_ms=30, min_db=-20.0)
    gate.arm()
    level = np.full(480, 0.1, dtype=np.float32)  # RMS 0.1 = -20 dBFS

    assert gate.observe(level) is True
    gate.arm()
    assert gate.observe(level * 0.9) is False  # a little under it


def test_pauses_between_words_do_not_reset_the_count() -> None:
    gate = BargeInGate(min_ms=90)
    gate.arm()

    assert [gate.observe(frame(a)) for a in (LOUD, QUIET, LOUD, QUIET, LOUD)] == [
        False,
        False,
        False,
        False,
        True,
    ]


def test_nothing_fires_while_it_is_not_armed() -> None:
    gate = BargeInGate()

    assert not any(gate.observe(frame(LOUD)) for _ in range(20))


def test_disarming_forgets_what_was_counted() -> None:
    gate = BargeInGate()
    gate.arm()
    for _ in range(4):
        gate.observe(frame(LOUD))
    gate.disarm()
    gate.arm()

    assert gate.loud_ms == 0
    assert [gate.observe(frame(LOUD)) for _ in range(6)].index(True) == 5  # all over again


def test_zero_milliseconds_is_the_old_behaviour_any_detected_speech_counts() -> None:
    gate = BargeInGate(min_ms=0)
    gate.arm()

    assert gate.observe(frame(QUIET)) is True


@pytest.mark.parametrize("kwargs", [{"min_ms": -1}, {"frame_ms": 0}])
def test_nonsense_numbers_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="min_ms"):
        BargeInGate(**kwargs)


@pytest.mark.parametrize(
    "field", [{"barge_in_min_ms": -1}, {"barge_in_min_ms": 5000}, {"barge_in_min_db": 3.0}]
)
def test_the_configuration_has_sane_bounds(field: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        PipelineConfig(**field)


# ---- the pipeline -----------------------------------------------------------------------------


class Rig:
    def __init__(self, **config: object) -> None:
        self.audio_in = FakeAudioInput()
        self.out = HeldOutput()
        self.events = Events()
        self.pipeline = DefaultVoicePipeline(
            audio_in=self.audio_in,
            audio_out=self.out,
            stt=FakeStt(),
            tts=FakeTts(),
            emit=self.events,
            capture_allowed=lambda: True,
            config=PipelineConfig(**config),  # type: ignore[arg-type]
            segmenter=Segmenter(sample_rate=16000, end_silence_ms=300, min_segment_ms=200),
        )

    async def speaking(self) -> asyncio.Task[None]:
        await self.pipeline.start()
        task = asyncio.create_task(
            self.pipeline.say(TtsRequest(utterance_id="s1", text="Eine lange Antwort."))
        )
        await self.out.playing.wait()
        return task

    async def feed(self, frames: list[np.ndarray]) -> None:
        for f in frames:
            self.audio_in.push(f)
            await settle()


async def test_loud_speech_over_nox_stops_the_answer() -> None:
    rig = Rig()
    task = await rig.speaking()

    await rig.feed(frames_of(tone(600, amp=LOUD)))
    await task

    assert rig.out.stops == ["barge_in"]
    assert rig.events.ids(E.TTS_INTERRUPTED) == ["s1"]
    await rig.pipeline.stop()


async def test_the_answer_is_cut_when_the_speech_has_lasted_long_enough_not_before() -> None:
    rig = Rig()
    task = await rig.speaking()
    frames = frames_of(tone(600, amp=LOUD))

    # The detector opens the segment on frame 3 and counts from there: frame 8 is the sixth loud
    # one it has seen since.
    await rig.feed(frames[:7])
    assert rig.out.stops == []
    await rig.feed(frames[7:8])
    await task

    assert rig.out.stops == ["barge_in"]
    await rig.pipeline.stop()


async def test_quiet_room_noise_that_the_detector_takes_for_speech_leaves_the_answer_alone() -> (
    None
):
    rig = Rig()
    task = await rig.speaking()

    await rig.feed(frames_of(tone(1500, amp=QUIET)))

    assert rig.events.of(E.VOICE_INPUT_STARTED)  # the detector did open a segment
    assert rig.out.stops == [] and not task.done()  # and the answer went on
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_a_short_loud_sound_such_as_a_cough_leaves_the_answer_alone() -> None:
    rig = Rig()
    task = await rig.speaking()

    await rig.feed(
        frames_of(tone(150, amp=LOUD))
    )  # five frames: the detector starts, the gate waits
    await rig.feed(frames_of(noise(900)))  # silence

    assert rig.events.of(E.VOICE_INPUT_STARTED)
    assert rig.out.stops == [] and not task.done()
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_a_short_sound_does_not_count_towards_the_next_one() -> None:
    rig = Rig()
    task = await rig.speaking()
    await rig.feed(frames_of(tone(150, amp=LOUD)))
    await rig.feed(frames_of(noise(900)))  # the segment ends here, what was counted is forgotten

    await rig.feed(frames_of(tone(150, amp=LOUD)))  # a second cough

    assert rig.out.stops == []
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_the_key_press_is_the_users_word_and_interrupts_at_once() -> None:
    rig = Rig()
    task = await rig.speaking()

    await rig.pipeline.push_to_talk(True)
    await asyncio.wait_for(task, 1)

    assert rig.out.stops == ["barge_in"]  # no frame of audio was needed
    await rig.pipeline.stop()


async def test_with_a_minimum_of_zero_any_detected_speech_interrupts_as_before() -> None:
    rig = Rig(barge_in_min_ms=0)
    task = await rig.speaking()

    await rig.feed(frames_of(tone(150, amp=QUIET)))
    await task

    assert rig.out.stops == ["barge_in"]
    await rig.pipeline.stop()


async def test_a_higher_threshold_asks_for_a_louder_voice() -> None:
    rig = Rig(barge_in_min_db=-5.0)
    task = await rig.speaking()

    await rig.feed(frames_of(tone(600, amp=0.1)))  # -23 dBFS: loud, but not that loud

    assert rig.out.stops == []
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_barge_in_can_be_switched_off() -> None:
    rig = Rig(barge_in=False)
    task = await rig.speaking()

    await rig.feed(frames_of(tone(600, amp=LOUD)))
    await rig.pipeline.push_to_talk(True)

    assert rig.out.stops == []
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_speech_that_began_before_nox_spoke_is_not_talked_over() -> None:
    """The user is already talking when Nox begins to answer something else: not a barge-in."""
    rig = Rig()
    await rig.pipeline.start()
    await rig.feed(frames_of(tone(300, amp=LOUD)))  # the segment opens with no speech to talk over
    task = asyncio.create_task(
        rig.pipeline.say(TtsRequest(utterance_id="s1", text="Eine Erinnerung."))
    )
    await rig.out.playing.wait()

    await rig.feed(frames_of(tone(600, amp=LOUD)))

    assert rig.out.stops == []
    rig.out.release.set()
    await task
    await rig.pipeline.stop()


async def test_an_interruption_that_already_happened_is_not_repeated_by_the_same_speech() -> None:
    rig = Rig()
    task = await rig.speaking()
    await rig.feed(frames_of(tone(600, amp=LOUD)))
    await task

    await rig.feed(frames_of(tone(600, amp=LOUD)))  # the user is still talking

    assert rig.out.stops == ["barge_in"]  # once
    await rig.pipeline.stop()
