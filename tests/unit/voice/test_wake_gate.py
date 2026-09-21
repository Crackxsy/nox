"""Wake-word gate decisions and detector selection (#20). No audio hardware, no real model."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nox.core.events import HealthStatus
from nox.voice.stt.wake_gate import (
    GateDecision,
    OpenWakeWordDetector,
    TextFallbackDetector,
    WakeGate,
    WakeGateConfig,
    build_detector,
    openwakeword_models,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeDetector:
    """Fires whenever a frame whose first sample exceeds `fire_above` is pushed."""

    id = "fake-wake"

    def __init__(self, *, fire_above: float = 0.5) -> None:
        self.fire_above = fire_above
        self.pushes = 0
        self.resets = 0

    @property
    def acoustic(self) -> bool:
        return True

    def health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.AVAILABLE, "fake"

    def push(self, audio: np.ndarray) -> float:
        self.pushes += 1
        return 1.0 if float(audio[0]) > self.fire_above else 0.0

    def reset(self) -> None:
        self.resets += 1


def gate(**config: float | bool) -> tuple[WakeGate, FakeDetector, FakeClock]:
    detector = FakeDetector()
    clock = FakeClock()
    return (
        WakeGate(detector=detector, config=WakeGateConfig(**config), clock=clock),  # type: ignore[arg-type]
        detector,
        clock,
    )


def quiet() -> np.ndarray:
    return np.zeros(480, dtype=np.float32)


def loud() -> np.ndarray:
    return np.ones(480, dtype=np.float32)


# ---- decisions ---------------------------------------------------------------------------------


def test_drops_segments_without_a_wake_word() -> None:
    g, _, _ = gate()
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP
    assert g.dropped == 1


def test_wake_word_opens_the_gate_for_the_window() -> None:
    g, _, clock = gate(wake_window_s=8.0)
    assert g.feed(loud()) is True
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.WAKE
    clock.advance(7.9)
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.WAKE
    clock.advance(0.2)
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP


def test_quiet_frames_do_not_open_the_gate() -> None:
    g, detector, _ = gate()
    assert g.feed(quiet()) is False
    assert detector.pushes == 1
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP


def test_ptt_always_passes_even_without_a_detection() -> None:
    g, _, _ = gate()
    assert g.decide(duration_ms=60000, ptt=True) is GateDecision.PTT


def test_conversation_window_lets_a_follow_up_through() -> None:
    g, _, clock = gate(conversation_window_s=20.0)
    g.note_addressed()
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.CONVERSATION
    clock.advance(21.0)
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP


def test_short_segments_reach_the_kill_phrase_watchdog() -> None:
    g, _, _ = gate(kill_watchdog_max_ms=2500)
    assert g.decide(duration_ms=1200, ptt=False) is GateDecision.KILL_WATCHDOG
    assert g.decide(duration_ms=2501, ptt=False) is GateDecision.DROP


def test_kill_watchdog_can_be_switched_off() -> None:
    g, _, _ = gate(kill_watchdog=False)
    assert g.decide(duration_ms=1200, ptt=False) is GateDecision.DROP


def test_text_fallback_passes_everything() -> None:
    g = WakeGate(detector=TextFallbackDetector())
    assert g.acoustic is False
    assert g.decide(duration_ms=60000, ptt=False) is GateDecision.TEXT_FALLBACK
    assert g.feed(loud()) is False  # nothing to feed a detector that does not exist


def test_reset_closes_both_windows() -> None:
    g, detector, _ = gate()
    g.feed(loud())
    g.note_addressed()
    g.reset()
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP
    assert detector.resets == 1


def test_threshold_is_respected() -> None:
    detector = FakeDetector()
    g = WakeGate(detector=detector, config=WakeGateConfig(threshold=1.5), clock=FakeClock())
    assert g.feed(loud()) is False  # score 1.0 < threshold 1.5
    assert g.decide(duration_ms=6000, ptt=False) is GateDecision.DROP


@pytest.mark.parametrize(
    ("decision", "passes", "watchdog"),
    [
        (GateDecision.PTT, True, False),
        (GateDecision.WAKE, True, False),
        (GateDecision.CONVERSATION, True, False),
        (GateDecision.KILL_WATCHDOG, True, True),
        (GateDecision.TEXT_FALLBACK, True, False),
        (GateDecision.DROP, False, False),
    ],
)
def test_decision_flags(decision: GateDecision, passes: bool, watchdog: bool) -> None:
    assert decision.passes is passes
    assert decision.watchdog_only is watchdog


# ---- detector selection ------------------------------------------------------------------------


def test_text_engine_never_builds_an_acoustic_detector(tmp_path: Path) -> None:
    detector = build_detector(engine="text", models_dir=tmp_path)
    assert detector.acoustic is False
    status, reason = detector.health()
    assert status is HealthStatus.LIMITED
    assert "text" in reason.lower() or "transcript" in reason.lower()


def test_missing_model_falls_back_to_text_with_a_limited_reason(tmp_path: Path) -> None:
    detector = build_detector(engine="openwakeword", models_dir=tmp_path / "nothing-here")
    assert detector.acoustic is False
    status, reason = detector.health()
    assert status is HealthStatus.LIMITED
    assert "nothing-here" in reason


def test_unloadable_model_falls_back_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / "not-really-a-model.onnx").write_bytes(b"nope")
    detector = build_detector(engine="openwakeword", models_dir=tmp_path)
    assert detector.acoustic is False
    assert detector.health()[0] is HealthStatus.LIMITED


def test_model_discovery(tmp_path: Path) -> None:
    (tmp_path / "b.onnx").write_bytes(b"")
    (tmp_path / "a.tflite").write_bytes(b"")
    (tmp_path / "readme.txt").write_bytes(b"")
    assert [p.name for p in openwakeword_models(tmp_path)] == ["a.tflite", "b.onnx"]
    assert [p.name for p in openwakeword_models(tmp_path, "b.onnx")] == ["b.onnx"]
    assert openwakeword_models(tmp_path, "missing.onnx") == []


def test_unloaded_openwakeword_detector_is_inert() -> None:
    detector = OpenWakeWordDetector([Path("nowhere.onnx")])
    assert detector.acoustic is False
    assert detector.health()[0] is HealthStatus.UNAVAILABLE
    assert detector.push(loud()) == 0.0
