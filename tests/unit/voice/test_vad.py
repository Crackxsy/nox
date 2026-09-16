"""VAD + segmenter on synthetic signals (no hardware)."""

from __future__ import annotations

import numpy as np

from nox.voice.vad import EnergyVad, Segmenter, SegmentKind, frame_rms_db, frame_zcr
from tests.unit.voice.conftest import FRAME, SR, frames_of, noise, tone


def test_levels() -> None:
    assert frame_rms_db(np.zeros(FRAME, dtype=np.float32)) < -100
    assert -10 < frame_rms_db(tone(30, amp=0.5)) < -5
    assert frame_zcr(np.ones(FRAME, dtype=np.float32)) == 0.0
    assert 0.02 < frame_zcr(tone(30)) < 0.05  # 220 Hz -> ~0.0275 crossings/sample


def test_vad_silence_and_noise_are_not_speech() -> None:
    vad = EnergyVad()
    assert not any(vad.is_speech(f) for f in frames_of(np.zeros(SR, dtype=np.float32)))
    assert not any(vad.is_speech(f) for f in frames_of(noise(1000, amp=0.001)))


def test_vad_tone_is_speech_and_floor_adapts() -> None:
    vad = EnergyVad()
    for f in frames_of(noise(600, amp=0.002)):
        vad.is_speech(f)
    floor_after_noise = vad.noise_floor_db
    assert -60 < floor_after_noise < -40
    speech = [vad.is_speech(f) for f in frames_of(tone(300, amp=0.2))]
    assert all(speech)
    assert vad.noise_floor_db == floor_after_noise  # speech frames never raise the floor


def test_vad_broadband_hiss_rejected_by_zcr() -> None:
    vad = EnergyVad(margin_db=3.0)
    hiss = noise(300, amp=0.3)  # loud white noise: zcr ~0.5
    assert not any(vad.is_speech(f) for f in frames_of(hiss))


def test_segmenter_produces_one_utterance_with_pre_roll() -> None:
    seg = Segmenter(end_silence_ms=300, pre_roll_ms=90, min_segment_ms=200)
    signal = np.concatenate([noise(600), tone(700), noise(600)])
    events = [e for e in (seg.push(f) for f in frames_of(signal)) if e is not None]
    kinds = [e.kind for e in events]
    assert kinds == [SegmentKind.START, SegmentKind.END]
    end = events[-1]
    assert end.audio is not None
    # 700 ms tone + 90 ms pre-roll (+ hangover) minus the trailing silence that is trimmed.
    assert 700 <= end.duration_ms <= 700 + 90 + 60
    assert not seg.active


def test_segmenter_drops_clicks() -> None:
    seg = Segmenter(end_silence_ms=200, min_segment_ms=400, start_frames=1, pre_roll_ms=60)
    signal = np.concatenate([noise(300), tone(120), noise(400)])
    events = [e for e in (seg.push(f) for f in frames_of(signal)) if e is not None]
    assert [e.kind for e in events] == [SegmentKind.START, SegmentKind.ABORT]


def test_segmenter_hard_max_length() -> None:
    seg = Segmenter(end_silence_ms=300, max_segment_ms=900)
    events = [e for e in (seg.push(f) for f in frames_of(tone(3000))) if e is not None]
    ends = [e for e in events if e.kind == SegmentKind.END]
    assert len(ends) >= 3
    assert all(e.duration_ms <= 900 for e in ends)


def test_segmenter_forced_ptt_ignores_vad() -> None:
    seg = Segmenter(end_silence_ms=200, min_segment_ms=100)
    start = seg.force_start()
    assert start is not None and start.kind == SegmentKind.START and start.forced
    for f in frames_of(np.zeros(SR, dtype=np.float32)):  # 1 s of silence, VAD would end it
        assert seg.push(f) is None
    end = seg.force_end()
    assert end is not None and end.kind == SegmentKind.END and end.forced
    assert end.audio is not None and 990 <= end.duration_ms <= 1010
    assert seg.force_end() is None
