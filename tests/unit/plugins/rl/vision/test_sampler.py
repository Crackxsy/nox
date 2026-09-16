"""`FrameSampler` (ST-18-04/05): confidence gating before an event ever reaches the bus, the budget
guard's auto-disable after consecutive over-budget samples, cooldown-gated auto-recovery, and the
manual override that always wins and never self-recovers (Spec v0.9 §4.4)."""

from __future__ import annotations

from typing import Any

import numpy as np
from nox_plugin_rl.vision.interface import Detection, DetectionResult, DetectorState
from nox_plugin_rl.vision.sampler import FrameSampler, VisionBudgetConfig

_FRAME = np.zeros((4, 4, 3), dtype=np.uint8)
_REGION = (0.0, 0.0, 1.0, 1.0)


class _FakeDetector:
    name = "fake"

    def __init__(
        self,
        *,
        process_time_ms: float = 0.0,
        detections: list[Detection] | None = None,
        state: tuple[DetectorState, str] = (DetectorState.AVAILABLE, "ok"),
    ) -> None:
        self.process_time_ms = process_time_ms
        self.detections = detections or []
        self._state = state
        self.calls = 0

    def detect(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> DetectionResult:
        self.calls += 1
        return DetectionResult(
            detections=list(self.detections), process_time_ms=self.process_time_ms
        )

    def state(self) -> tuple[DetectorState, str]:
        return self._state


async def test_sample_gates_low_confidence_detections_and_emits_only_the_rest() -> None:
    high = Detection(entity="ball", confidence=0.9, x=0.1, y=0.1, w=0.05, h=0.05)
    low = Detection(entity="car", confidence=0.1, x=0.2, y=0.2, w=0.05, h=0.05, team="self")
    detector = _FakeDetector(detections=[high, low])
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    sampler = FrameSampler(detector, config=VisionBudgetConfig(min_confidence=0.5), emit=emit)
    gated = await sampler.sample(_FRAME, arena_region=_REGION)

    assert [d.entity for d in gated] == ["ball"]
    assert emitted and emitted[0][0] == "rl.vision.detections"
    assert emitted[0][1]["detections"][0]["entity"] == "ball"


async def test_no_detections_above_threshold_emits_nothing() -> None:
    low = Detection(entity="ball", confidence=0.1, x=0.1, y=0.1, w=0.05, h=0.05)
    detector = _FakeDetector(detections=[low])
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    sampler = FrameSampler(detector, config=VisionBudgetConfig(min_confidence=0.5), emit=emit)
    gated = await sampler.sample(_FRAME, arena_region=_REGION)

    assert gated == []
    assert emitted == []


async def test_budget_guard_disables_after_consecutive_over_budget_samples() -> None:
    # sample_hz=10 -> 100ms interval; max_process_fraction=0.5 -> 50ms threshold; always over.
    detector = _FakeDetector(process_time_ms=80.0)
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        emitted.append((name, payload))

    cfg = VisionBudgetConfig(sample_hz=10.0, max_process_fraction=0.5, consecutive_over_budget=3)
    sampler = FrameSampler(detector, config=cfg, emit=emit)

    for _ in range(3):
        await sampler.sample(_FRAME, arena_region=_REGION)

    assert sampler.active is False
    disabled = [e for e in emitted if e[0] == "rl.vision.disabled"]
    assert len(disabled) == 1
    assert disabled[0][1]["automatic"] is True
    assert "budget" in disabled[0][1]["reason"]

    calls_before = detector.calls
    result = await sampler.sample(_FRAME, arena_region=_REGION)
    assert result == []
    assert detector.calls == calls_before  # disabled: the detector is never called again


async def test_budget_guard_ignores_a_single_spike() -> None:
    detector = _FakeDetector(process_time_ms=80.0)
    cfg = VisionBudgetConfig(sample_hz=10.0, max_process_fraction=0.5, consecutive_over_budget=3)
    sampler = FrameSampler(detector, config=cfg)

    await sampler.sample(_FRAME, arena_region=_REGION)
    await sampler.sample(_FRAME, arena_region=_REGION)
    detector.process_time_ms = 1.0  # back under budget before the streak completes
    await sampler.sample(_FRAME, arena_region=_REGION)

    assert sampler.active is True


async def test_budget_guard_recovers_only_after_the_cooldown_elapses() -> None:
    detector = _FakeDetector(process_time_ms=80.0)
    now = {"t": 0.0}
    cfg = VisionBudgetConfig(
        sample_hz=10.0, max_process_fraction=0.5, consecutive_over_budget=2, cooldown_s=5.0
    )
    sampler = FrameSampler(detector, config=cfg, now_fn=lambda: now["t"])

    await sampler.sample(_FRAME, arena_region=_REGION)
    await sampler.sample(_FRAME, arena_region=_REGION)
    assert sampler.active is False

    now["t"] += 4.9
    await sampler.sample(_FRAME, arena_region=_REGION)
    assert sampler.active is False  # still within the cooldown window

    now["t"] += 1.0
    detector.process_time_ms = 1.0  # cheap again once the cooldown clears
    await sampler.sample(_FRAME, arena_region=_REGION)
    assert sampler.active is True


async def test_manual_disable_overrides_and_never_auto_recovers() -> None:
    detector = _FakeDetector(process_time_ms=0.0)
    sampler = FrameSampler(detector, config=VisionBudgetConfig(sample_hz=10.0))

    sampler.disable_manually()
    assert sampler.active is False
    result = await sampler.sample(_FRAME, arena_region=_REGION)
    assert result == []
    assert detector.calls == 0

    sampler.enable_manually()
    assert sampler.active is True


def test_state_reports_manual_and_budget_disable_reasons_distinctly() -> None:
    detector = _FakeDetector(state=(DetectorState.AVAILABLE, "ok"))
    sampler = FrameSampler(detector)

    sampler.disable_manually()
    state, reason = sampler.state()
    assert state == DetectorState.DISABLED
    assert "manually" in reason

    sampler.enable_manually()
    state, reason = sampler.state()
    assert (state, reason) == (DetectorState.AVAILABLE, "ok")
