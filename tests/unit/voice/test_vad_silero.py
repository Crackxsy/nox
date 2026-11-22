"""The neural speech detector: block handling, hysteresis, fallback, model checks.

Logic tests run against a scripted session, so they need no onnxruntime. The tests at the bottom
load the real model (the copy inside faster-whisper) and are skipped where it is not installed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nox.core.events import HealthStatus
from nox.voice import vad_silero
from nox.voice.vad import EnergyVad, Segmenter, SegmentKind
from nox.voice.vad_silero import (
    BLOCK_SAMPLES,
    CONTEXT_SAMPLES,
    ResilientVad,
    SileroModelError,
    SileroParams,
    SileroVad,
    build_vad,
    find_model,
    verify_model,
)
from tests.unit.voice import audio_fixtures as fx

FRAME = fx.SR * 30 // 1000


class ScriptedSession:
    """Answers each block with the next scripted probability and records what it was given."""

    def __init__(self, probabilities: list[float] | None = None, *, default: float = 0.0) -> None:
        self._script: Iterator[float] = iter(probabilities or [])
        self._default = default
        self.rows: list[np.ndarray] = []
        self.states: list[tuple[np.ndarray, np.ndarray]] = []
        self.calls = 0
        self.fail: Exception | None = None

    def run(self, output_names: Any, input_feed: Any) -> list[np.ndarray]:
        if self.fail is not None:
            raise self.fail
        self.calls += 1
        self.rows.append(np.array(input_feed["input"]))
        self.states.append((np.array(input_feed["h"]), np.array(input_feed["c"])))
        probability = next(self._script, self._default)
        h = input_feed["h"] + 1.0  # the state visibly changes with every block
        return [np.array([probability], dtype=np.float32), h, input_feed["c"] + 2.0]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.step = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def block(value: float = 0.1) -> np.ndarray:
    return np.full(BLOCK_SAMPLES, value, dtype=np.float32)


# ---- blocks and state ---------------------------------------------------------------------------


def test_frames_are_collected_into_whole_blocks_of_512_samples() -> None:
    session = ScriptedSession()
    vad = SileroVad(session)
    for _ in range(16):  # 16 frames of 480 samples = 7680 samples = exactly 15 blocks
        vad.is_speech(np.zeros(FRAME, dtype=np.float32))
    assert session.calls == 15
    assert vad.blocks == 15
    vad.is_speech(np.zeros(FRAME, dtype=np.float32))
    assert session.calls == 15  # 480 samples are left over, not yet a block
    vad.is_speech(np.zeros(FRAME, dtype=np.float32))
    assert session.calls == 16  # 960 left over completes the next block


def test_a_frame_that_completes_no_block_repeats_the_previous_probability() -> None:
    session = ScriptedSession([0.9])
    vad = SileroVad(session)
    assert vad.is_speech(np.zeros(300, dtype=np.float32)) is False  # 300 < 512: no block yet
    assert session.calls == 0
    assert vad.is_speech(np.zeros(300, dtype=np.float32)) is True  # block 1 completes: 0.9
    assert vad.is_speech(np.zeros(100, dtype=np.float32)) is True  # no block: still speech
    assert session.calls == 1


def test_every_block_is_given_the_64_samples_before_it_as_context() -> None:
    session = ScriptedSession()
    vad = SileroVad(session)
    first = np.arange(BLOCK_SAMPLES, dtype=np.float32)
    second = first + 1000.0
    vad.is_speech(np.concatenate([first, second]))
    assert [row.shape for row in session.rows] == [(1, 576), (1, 576)]
    assert np.all(session.rows[0][0, :CONTEXT_SAMPLES] == 0.0)  # nothing before the first block
    assert np.array_equal(session.rows[0][0, CONTEXT_SAMPLES:], first)
    assert np.array_equal(session.rows[1][0, :CONTEXT_SAMPLES], first[-CONTEXT_SAMPLES:])
    assert np.array_equal(session.rows[1][0, CONTEXT_SAMPLES:], second)


def test_the_recurrent_state_is_carried_from_block_to_block() -> None:
    session = ScriptedSession()
    vad = SileroVad(session)
    vad.is_speech(np.zeros(BLOCK_SAMPLES * 3, dtype=np.float32))
    assert [float(h.max()) for h, _ in session.states] == [0.0, 1.0, 2.0]
    assert [float(c.max()) for _, c in session.states] == [0.0, 2.0, 4.0]


def test_reset_starts_from_the_initial_state() -> None:
    session = ScriptedSession([0.9])
    vad = SileroVad(session)
    assert vad.is_speech(block()) is True
    vad.reset()
    assert vad.probability == 0.0
    assert vad.is_speech(block()) is False  # no more scripted probabilities: 0.0
    assert float(session.states[-1][0].max()) == 0.0
    assert np.all(session.rows[-1][0, :CONTEXT_SAMPLES] == 0.0)


def test_a_long_stretch_without_speech_clears_the_state() -> None:
    session = ScriptedSession()
    vad = SileroVad(session, SileroParams(reset_after_silence_s=1.0))
    for _ in range(40):  # 1.2 s of non-speech
        vad.is_speech(np.zeros(FRAME, dtype=np.float32))
    assert float(session.states[-1][0].max()) < 25.0  # it was cleared along the way
    cleared = [float(h.max()) for h, _ in session.states]
    assert cleared.count(0.0) >= 2  # the start, and again after the second


def test_speech_keeps_the_state_from_being_cleared() -> None:
    session = ScriptedSession(default=0.9)
    vad = SileroVad(session, SileroParams(reset_after_silence_s=1.0))
    for _ in range(80):
        vad.is_speech(np.zeros(FRAME, dtype=np.float32))
    assert [float(h.max()) for h, _ in session.states].count(0.0) == 1


def test_only_16_khz_is_accepted() -> None:
    with pytest.raises(ValueError, match="16 kHz"):
        SileroVad(ScriptedSession(), sample_rate=8000)


# ---- the decision -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probability", "expected"),
    [(0.0, False), (0.49, False), (0.5, True), (0.51, True), (1.0, True)],
)
def test_speech_starts_exactly_at_the_threshold(probability: float, expected: bool) -> None:
    vad = SileroVad(ScriptedSession([probability]))
    assert vad.is_speech(block()) is expected


@pytest.mark.parametrize(
    ("probability", "still_speech"),
    [(0.99, True), (0.5, True), (0.36, True), (0.34, False), (0.0, False)],
)
def test_speech_ends_only_below_the_lower_threshold(probability: float, still_speech: bool) -> None:
    vad = SileroVad(ScriptedSession([0.9, probability]))
    assert vad.is_speech(block()) is True
    assert vad.is_speech(block()) is still_speech


def test_a_probability_exactly_on_the_lower_threshold_still_counts_as_speech() -> None:
    params = SileroParams(threshold=0.75, threshold_off=0.25)  # exact in binary floating point
    vad = SileroVad(ScriptedSession([0.9, 0.25, 0.24]), params)
    assert [vad.is_speech(block()) for _ in range(3)] == [True, True, False]


def test_a_probability_between_the_thresholds_changes_nothing() -> None:
    vad = SileroVad(ScriptedSession([0.45, 0.9, 0.45, 0.2, 0.45]))
    decisions = [vad.is_speech(block()) for _ in range(5)]
    assert decisions == [False, True, True, False, False]


def test_the_loudest_block_of_a_frame_decides() -> None:
    vad = SileroVad(ScriptedSession([0.1, 0.9]))
    assert vad.is_speech(np.zeros(BLOCK_SAMPLES * 2, dtype=np.float32)) is True
    assert vad.probability == pytest.approx(0.9)


def test_the_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="threshold_off"):
        SileroParams(threshold=0.4, threshold_off=0.5)
    with pytest.raises(ValueError, match="threshold_off"):
        SileroParams(threshold=1.0)
    SileroParams(threshold=0.5, threshold_off=0.5)  # equal is allowed


# ---- the time budget ----------------------------------------------------------------------------


def test_blocks_over_the_budget_are_counted_and_a_fast_one_clears_the_run() -> None:
    clock = FakeClock()
    vad = SileroVad(ScriptedSession(), SileroParams(budget_ms=40.0), clock=clock)
    clock.step = 0.050  # every clock read is 50 ms later: each block "takes" 50 ms
    vad.is_speech(np.zeros(BLOCK_SAMPLES * 3, dtype=np.float32))
    assert vad.slow_run == 3
    assert vad.max_block_ms == pytest.approx(50.0)
    clock.step = 0.010
    vad.is_speech(block())
    assert vad.slow_run == 0


def test_a_block_exactly_on_the_budget_is_not_over_it() -> None:
    clock = FakeClock()
    clock.step = 0.040
    vad = SileroVad(ScriptedSession(), SileroParams(budget_ms=40.0), clock=clock)
    vad.is_speech(block())
    assert vad.slow_run == 0


def test_the_budget_is_missed_for_good_after_the_configured_run() -> None:
    clock = FakeClock()
    clock.step = 0.100
    vad = SileroVad(ScriptedSession(), SileroParams(slow_blocks_to_degrade=3), clock=clock)
    for _ in range(2):
        vad.is_speech(block())
    assert vad.over_budget is False
    vad.is_speech(block())
    assert vad.over_budget is True


# ---- the energy detector behind it --------------------------------------------------------------


def test_a_failing_model_hands_over_to_the_energy_detector_and_says_why() -> None:
    session = ScriptedSession(default=0.9)
    resilient = ResilientVad(SileroVad(session), EnergyVad())
    assert resilient.engine == "silero"
    assert resilient.health() == (HealthStatus.AVAILABLE, "speech detection: Silero VAD")
    assert resilient.is_speech(block()) is True
    session.fail = RuntimeError("session gone")

    loud = fx.speech_like(BLOCK_SAMPLES / fx.SR)  # one whole block, so the model is asked
    assert resilient.is_speech(loud) is True  # the energy detector answers: speech-like and loud
    assert resilient.engine == "energy"
    status, text = resilient.health()
    assert status is HealthStatus.LIMITED
    assert "energy fallback" in text
    assert "RuntimeError: session gone" in text
    assert resilient.degraded_reason.startswith("RuntimeError")


def test_a_detector_that_is_too_slow_hands_over_too() -> None:
    clock = FakeClock()
    clock.step = 0.100
    primary = SileroVad(
        ScriptedSession(default=0.9), SileroParams(slow_blocks_to_degrade=2), clock=clock
    )
    resilient = ResilientVad(primary, EnergyVad())
    resilient.is_speech(block())
    assert resilient.engine == "silero"
    resilient.is_speech(block())
    assert resilient.engine == "energy"
    assert "2 blocks in a row over 40 ms" in resilient.degraded_reason


def test_without_a_primary_the_energy_detector_answers_with_the_reason() -> None:
    resilient = ResilientVad(None, EnergyVad(), reason="onnxruntime is not installed")
    assert resilient.engine == "energy"
    assert resilient.health() == (
        HealthStatus.LIMITED,
        "speech detection: energy fallback (onnxruntime is not installed)",
    )
    assert resilient.is_speech(np.zeros(FRAME, dtype=np.float32)) is False
    assert ResilientVad(None, EnergyVad()).degraded_reason == "no neural detector"


def test_reset_resets_both_detectors() -> None:
    session = ScriptedSession(default=0.9)
    energy = EnergyVad()
    energy.noise_floor_db = -30.0
    primary = SileroVad(session)
    resilient = ResilientVad(primary, energy)
    resilient.is_speech(block())
    resilient.reset()
    assert primary.probability == 0.0
    assert energy.noise_floor_db == -60.0


# ---- finding and trusting the model -------------------------------------------------------------


def test_an_explicit_model_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SileroModelError, match="does not exist"):
        find_model(str(tmp_path / "nope.onnx"), tmp_path)


def test_a_relative_model_is_looked_for_in_the_models_folder(tmp_path: Path) -> None:
    (tmp_path / "mine.onnx").write_bytes(b"x")
    assert find_model("mine.onnx", tmp_path) == tmp_path / "mine.onnx"


def test_the_models_folder_comes_before_the_bundled_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled.onnx"
    bundled.write_bytes(b"b")
    monkeypatch.setattr(vad_silero, "bundled_model_path", lambda: bundled)
    assert find_model("", tmp_path) == bundled  # nothing in the folder yet
    (tmp_path / vad_silero.MODEL_FILE).write_bytes(b"f")
    assert find_model("", tmp_path) == tmp_path / vad_silero.MODEL_FILE


def test_an_explicit_model_does_not_fall_back_to_the_bundled_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled.onnx"
    bundled.write_bytes(b"b")
    monkeypatch.setattr(vad_silero, "bundled_model_path", lambda: bundled)
    with pytest.raises(SileroModelError):
        find_model("missing.onnx", tmp_path)


def test_no_model_anywhere_is_an_error_that_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vad_silero, "bundled_model_path", lambda: None)
    with pytest.raises(SileroModelError, match=vad_silero.MODEL_FILE):
        find_model("", tmp_path)


def test_only_a_model_with_a_known_hash_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "m.onnx"
    path.write_bytes(b"not a model")
    digest = hashlib.sha256(b"not a model").hexdigest()
    with pytest.raises(SileroModelError, match=digest):
        verify_model(path)
    verify_model(path, known=frozenset({digest}))  # no error


def test_the_pinned_hash_is_a_sha256() -> None:
    assert vad_silero.KNOWN_MODEL_SHA256
    assert all(len(h) == 64 and int(h, 16) >= 0 for h in vad_silero.KNOWN_MODEL_SHA256)


def test_open_session_refuses_a_file_with_an_unknown_hash_before_parsing_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m.onnx"
    path.write_bytes(b"\x00garbage")
    with pytest.raises(SileroModelError, match="not a known Silero VAD build"):
        vad_silero.open_session(path)


# ---- build_vad ----------------------------------------------------------------------------------


def test_the_energy_engine_is_a_deliberate_choice_not_a_failure(tmp_path: Path) -> None:
    vad = build_vad(engine="energy", models_dir=tmp_path)
    assert vad.engine == "energy"
    assert vad.health() == (
        HealthStatus.AVAILABLE,
        "speech detection: energy (chosen in the settings)",
    )


def test_a_missing_model_gives_the_energy_detector_and_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vad_silero, "bundled_model_path", lambda: None)
    vad = build_vad(models_dir=tmp_path)
    assert vad.engine == "energy"
    assert vad.health()[0] is HealthStatus.LIMITED
    assert vad_silero.MODEL_FILE in vad.health()[1]


def test_a_model_that_cannot_be_opened_gives_the_energy_detector(tmp_path: Path) -> None:
    (tmp_path / vad_silero.MODEL_FILE).write_bytes(b"x")

    def refuse(_path: Path) -> Any:
        raise SileroModelError("onnxruntime is not installed (voice extra)")

    vad = build_vad(models_dir=tmp_path, open_model=refuse)
    assert vad.engine == "energy"
    assert "onnxruntime is not installed" in vad.health()[1]


def test_a_usable_model_gives_the_neural_detector_with_the_configured_thresholds(
    tmp_path: Path,
) -> None:
    (tmp_path / vad_silero.MODEL_FILE).write_bytes(b"x")
    session = ScriptedSession([0.6, 0.4, 0.3])
    vad = build_vad(
        models_dir=tmp_path,
        params=SileroParams(threshold=0.6, threshold_off=0.4),
        open_model=lambda _p: session,
    )
    assert vad.engine == "silero"
    assert [vad.is_speech(block()) for _ in range(3)] == [True, True, False]


# ---- the real model -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real() -> ResilientVad:
    pytest.importorskip("onnxruntime")
    try:
        vad_silero.find_model("", None)
    except SileroModelError:
        pytest.skip("no Silero model (faster-whisper is not installed)")
    detector = build_vad()
    assert detector.engine == "silero", detector.degraded_reason
    return detector


def speech_fraction(vad: ResilientVad, signal: np.ndarray) -> float:
    vad.reset()
    decisions = [vad.is_speech(frame) for frame in fx.frames(signal)]
    return sum(decisions) / len(decisions)


def test_real_model_hears_speech_and_nothing_else(real: ResilientVad) -> None:
    assert speech_fraction(real, fx.speech_like(3.0)) > 0.85
    assert speech_fraction(real, fx.speech_like(3.0, f0=200.0, seed=4)) > 0.85
    assert speech_fraction(real, fx.silence(3.0)) == 0.0
    assert speech_fraction(real, fx.hiss(3.0, amp=0.2)) == 0.0


def test_real_model_is_not_fooled_by_music_or_game_sound_that_fools_the_energy_detector(
    real: ResilientVad,
) -> None:
    for signal in (fx.music_like(4.0), fx.game_like(4.0)):
        assert speech_fraction(real, signal) < 0.05
        energy = ResilientVad(None, EnergyVad(), reason="x")
        assert speech_fraction(energy, signal) > 0.8  # the problem this detector solves


def test_real_model_hears_speech_that_is_quiet(real: ResilientVad) -> None:
    quiet = fx.scaled_to_db(fx.speech_like(3.0), -45.0)
    assert speech_fraction(real, quiet) > 0.85


def test_real_model_makes_one_utterance_of_speech_between_silences(real: ResilientVad) -> None:
    real.reset()
    segmenter = Segmenter(vad=real, end_silence_ms=300, pre_roll_ms=90, min_segment_ms=250)
    signal = np.concatenate([fx.silence(0.6), fx.speech_like(1.5), fx.silence(0.8)])
    events = [e for e in (segmenter.push(f) for f in fx.frames(signal)) if e is not None]
    assert [e.kind for e in events] == [SegmentKind.START, SegmentKind.END]
    assert 1500 <= events[-1].duration_ms <= 1500 + 90 + 90


def test_real_model_stays_inside_the_latency_budget(real: ResilientVad) -> None:
    real.reset()
    primary = real._primary
    assert primary is not None
    primary.max_block_ms = 0.0
    for frame in fx.frames(fx.speech_like(2.0)):
        real.is_speech(frame)
    assert primary.max_block_ms < 40.0


def test_resetting_the_segmenter_starts_the_detector_afresh() -> None:
    session = ScriptedSession(default=0.9)
    vad = SileroVad(session)
    segmenter = Segmenter(vad=vad)
    segmenter.push(np.zeros(BLOCK_SAMPLES, dtype=np.float32))
    assert vad.probability == pytest.approx(0.9)
    segmenter.reset()
    assert vad.probability == 0.0
    assert float(vad._h.max()) == 0.0
