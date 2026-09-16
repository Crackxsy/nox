"""The wake-word gate inside the pipeline (#20): what Whisper is and is not allowed to see.

The point of these tests is the negative one - a segment that did not pass the gate must never
reach the STT engine at all (`stt.calls` stays empty), not merely fail to produce an event.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from nox.core.events import E, HealthStatus, validate_payload
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.stt.wake_gate import WakeGate, WakeGateConfig
from nox.voice.vad import Segmenter
from tests.unit.voice.conftest import (
    FRAME,
    FakeAudioInput,
    FakeAudioOutput,
    FakeStt,
    FakeTts,
    frames_of,
    noise,
    settle,
    tone,
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any]) -> None:
        validate_payload(name, payload)
        self.events.append((name, payload))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]


class FakeAcousticDetector:
    """Stands in for openWakeWord: fires on a constant-1.0 frame, which the VAD ignores as noise."""

    id = "fake-openwakeword"

    def __init__(self) -> None:
        self.pushes = 0

    @property
    def acoustic(self) -> bool:
        return True

    def health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.AVAILABLE, "fake acoustic detector"

    def push(self, audio: np.ndarray) -> float:
        self.pushes += 1
        return 1.0 if float(np.min(audio)) > 0.5 else 0.0

    def reset(self) -> None:
        return None


class FakeClock:
    """Lets a test step past `wake_window_s` without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wake_frame() -> np.ndarray:
    """Loud but with zero crossings below the VAD band, so it fires the detector, not the VAD."""
    return np.ones(FRAME, dtype=np.float32)


def short_utterance() -> list[np.ndarray]:
    """~1 s of speech - short enough for the kill-phrase watchdog."""
    return frames_of(np.concatenate([noise(300), tone(600), noise(500)]))


def long_utterance() -> list[np.ndarray]:
    """~4 s of speech - past `kill_watchdog_max_ms`, so only the gate can let it through."""
    return frames_of(np.concatenate([noise(300), tone(4000), noise(500)]))


def make_pipeline(
    audio_in: FakeAudioInput,
    audio_out: FakeAudioOutput,
    *,
    stt: FakeStt | None = None,
    acoustic: bool = True,
    gate_config: WakeGateConfig | None = None,
    clock: FakeClock | None = None,
    **config: Any,
) -> tuple[DefaultVoicePipeline, Recorder, FakeStt]:
    rec = Recorder()
    engine = stt or FakeStt()
    detector: Any = FakeAcousticDetector() if acoustic else None
    gate = WakeGate(config=gate_config or WakeGateConfig())
    if clock is not None:
        gate.clock = clock
    if detector is not None:
        gate.detector = detector
    p = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=engine,
        tts=FakeTts(),
        emit=rec,
        capture_allowed=lambda: True,
        config=PipelineConfig(**config),
        segmenter=Segmenter(
            sample_rate=16000, end_silence_ms=300, min_segment_ms=200, pre_roll_ms=60
        ),
        wake_gate=gate,
    )
    return p, rec, engine


# ---- continuous mode -----------------------------------------------------------------------------


async def test_segment_without_wake_word_never_reaches_whisper(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(audio_in, audio_out)
    await p.start()
    audio_in.push_all(long_utterance())
    await settle(60)
    assert stt.calls == []  # the whole point: not transcribed at all
    assert E.VOICE_TRANSCRIPT_READY not in rec.names()
    assert p.gated_out == 1
    assert p.utterances == 0
    await p.stop()


async def test_wake_word_lets_the_next_segment_through(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(audio_in, audio_out, stt=FakeStt("wie spät ist es"))
    await p.start()
    audio_in.push(wake_frame())
    await settle(5)
    audio_in.push_all(long_utterance())
    await settle(60)
    assert len(stt.calls) == 1
    assert E.VOICE_TRANSCRIPT_READY in rec.names()
    assert p.gated_out == 0
    await p.stop()


async def test_wake_word_marks_the_utterance_as_addressed(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """The acoustic detector heard the name, so the transcript need not repeat it."""
    p, rec, _ = make_pipeline(audio_in, audio_out, stt=FakeStt("wie spät ist es"))
    await p.start()
    audio_in.push(wake_frame())
    await settle(5)
    audio_in.push_all(long_utterance())
    await settle(60)
    ready = [payload for name, payload in rec.events if name == E.VOICE_TRANSCRIPT_READY][-1]
    assert ready["addressed_to_nox"] is True
    await p.stop()


async def test_conversation_window_carries_a_follow_up(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    clock = FakeClock()
    p, rec, stt = make_pipeline(
        audio_in,
        audio_out,
        stt=FakeStt("wie spät ist es"),
        gate_config=WakeGateConfig(wake_window_s=8.0, conversation_window_s=60.0),
        clock=clock,
    )
    await p.start()
    audio_in.push(wake_frame())
    await settle(5)
    audio_in.push_all(long_utterance())
    await settle(60)
    assert len(stt.calls) == 1
    assert p.wake_gate.conversation_open is True
    clock.advance(30.0)  # the wake window has long expired, the conversation window has not
    audio_in.push_all(long_utterance())  # no new wake word
    await settle(60)
    assert len(stt.calls) == 2
    assert rec.names().count(E.VOICE_TRANSCRIPT_READY) == 2
    clock.advance(120.0)  # now the conversation window is closed as well
    audio_in.push_all(long_utterance())
    await settle(60)
    assert len(stt.calls) == 2
    await p.stop()


async def test_ptt_bypasses_the_gate_entirely(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(audio_in, audio_out, stt=FakeStt("wie spät ist es"))
    await p.start()
    await p.push_to_talk(True)
    audio_in.push_all(long_utterance())
    await settle(10)
    await p.push_to_talk(False)
    await settle(60)
    assert len(stt.calls) == 1
    assert E.VOICE_TRANSCRIPT_READY in rec.names()
    await p.stop()


# ---- kill phrase ---------------------------------------------------------------------------------


async def test_kill_phrase_reaches_stt_without_a_wake_word(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """No acoustic model for "Nox Notaus" exists, so short segments stay on the watchdog path."""
    p, rec, stt = make_pipeline(audio_in, audio_out, stt=FakeStt("Nox Notaus"))
    await p.start()
    audio_in.push_all(short_utterance())
    await settle(60)
    assert len(stt.calls) == 1
    assert E.VOICE_KILL_PHRASE in rec.names()
    assert p.kill_latched
    await p.stop()


async def test_watchdog_discards_everything_that_is_not_the_kill_phrase(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(audio_in, audio_out, stt=FakeStt("Nox wie spät ist es"))
    await p.start()
    audio_in.push_all(short_utterance())
    await settle(60)
    assert len(stt.calls) == 1  # transcribed for the watchdog ...
    assert E.VOICE_TRANSCRIPT_READY not in rec.names()  # ... but never reported
    assert E.VOICE_KILL_PHRASE not in rec.names()
    assert p.gated_out == 1
    await p.stop()


async def test_watchdog_off_drops_short_segments_before_whisper(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, _, stt = make_pipeline(audio_in, audio_out, gate_config=WakeGateConfig(kill_watchdog=False))
    await p.start()
    audio_in.push_all(short_utterance())
    await settle(60)
    assert stt.calls == []
    await p.stop()


# ---- ptt_only ------------------------------------------------------------------------------------


async def test_ptt_only_never_opens_the_microphone_on_its_own(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(audio_in, audio_out, listening_mode="ptt_only")
    await p.start()
    assert audio_in.enabled is False
    audio_in.push_all(long_utterance(), force=True)  # even leaked frames are dropped
    audio_in.push_all(short_utterance(), force=True)
    await settle(60)
    assert p.frames_seen == 0
    assert stt.calls == []
    assert rec.events == []
    await p.stop()


async def test_ptt_only_opens_the_microphone_while_ptt_is_held(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(
        audio_in, audio_out, stt=FakeStt("wie spät ist es"), listening_mode="ptt_only"
    )
    await p.start()
    await p.push_to_talk(True)
    assert audio_in.enabled is True
    audio_in.push_all(short_utterance())
    await settle(10)
    await p.push_to_talk(False)
    await settle(60)
    assert audio_in.enabled is False  # closed again on release
    assert len(stt.calls) == 1
    assert E.VOICE_TRANSCRIPT_READY in rec.names()
    await p.stop()


async def test_kill_phrase_works_in_ptt_only_mode(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    p, rec, stt = make_pipeline(
        audio_in, audio_out, stt=FakeStt("Nox Notaus"), listening_mode="ptt_only"
    )
    await p.start()
    await p.push_to_talk(True)
    audio_in.push_all(short_utterance())
    await settle(10)
    await p.push_to_talk(False)
    await settle(60)
    assert len(stt.calls) == 1
    assert E.VOICE_KILL_PHRASE in rec.names()
    assert p.kill_latched
    await p.stop()


# ---- fallback ------------------------------------------------------------------------------------


async def test_text_fallback_keeps_the_previous_behaviour(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """Without an acoustic detector every segment is transcribed, as before #20."""
    p, rec, stt = make_pipeline(audio_in, audio_out, acoustic=False)
    await p.start()
    assert p.wake_gate_health()[0] is HealthStatus.LIMITED
    audio_in.push_all(long_utterance())
    await settle(60)
    assert len(stt.calls) == 1
    assert E.VOICE_TRANSCRIPT_READY in rec.names()
    assert p.gated_out == 0
    await p.stop()
