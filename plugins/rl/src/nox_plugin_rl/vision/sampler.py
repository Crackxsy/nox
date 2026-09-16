"""Frame sampler + budget guard (Spec v0.9 §4.4, ST-18-04/05): runs the configured detector at a
low, configurable rate downstream of the shared capture pipeline (it never captures a frame itself -
the caller drives frames in via `sample()`, mirroring `RlPlugin._recognize_loop`'s injected-capture
pattern so tests never need a real screen). Confidence-gates detections before they ever reach the
event bus (silence over guessing, FR-10.3) and measures its OWN processing time each sample - the
only budget signal available without a live GPU counter or PresentMon (SP-05 gates that; SP-19 folds
its measured CPU/GPU cost on synthetic 1080p frames into this guard's default budget). Crossing the
configured budget for `consecutive_over_budget` samples in a row disables Stage 2 automatically,
before ever touching Stage 1's own capture/recognize loop (FR-10.4 priority order); re-enable
requires the budget to stay clear for a full cooldown window (anti-flapping, Spec §4.4 step 4). A
manual disable always overrides everything and never auto-recovers on its own."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .interface import Detection, DetectorState, VisionDetector

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class VisionBudgetConfig:
    sample_hz: float = 1.0
    #: detection processing time / sample interval, above which one sample counts as "over budget".
    max_process_fraction: float = 0.5
    consecutive_over_budget: int = 3
    cooldown_s: float = 60.0
    min_confidence: float = 0.5


class FrameSampler:
    """Owns the sample loop's *decision* logic (whether to run the detector this tick, when to
    auto-disable/auto-recover); the plugin's own asyncio loop calls `sample()` once per tick."""

    def __init__(
        self,
        detector: VisionDetector,
        *,
        config: VisionBudgetConfig | None = None,
        emit: EmitFn | None = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = detector
        self._cfg = config or VisionBudgetConfig()
        self._emit = emit
        self._now = now_fn
        self._manual_disabled = False
        self._budget_disabled = False
        self._disabled_at: float | None = None
        self._over_budget_streak = 0
        self._recent_process_ms: deque[float] = deque(maxlen=20)

    @property
    def active(self) -> bool:
        """True only while neither a manual override nor the budget guard is holding Stage 2 off."""
        return not (self._manual_disabled or self._budget_disabled)

    @property
    def backend_name(self) -> str:
        return self._detector.name

    def disable_manually(self) -> None:
        self._manual_disabled = True

    def enable_manually(self) -> None:
        """Clears BOTH the manual override and any budget-guard disable (Spec §4.4: re-enable is
        available, subject to the cooldown already having been honoured by the budget guard
        itself - a manual re-enable while still mid-cooldown simply resumes sampling immediately,
        matching the spec's "manual override... remains available at all times" - P1 user control
        takes precedence over the anti-flapping cooldown)."""
        self._manual_disabled = False
        self._budget_disabled = False
        self._disabled_at = None
        self._over_budget_streak = 0

    def state(self) -> tuple[DetectorState, str]:
        if self._manual_disabled:
            return DetectorState.DISABLED, "manually disabled"
        if self._budget_disabled:
            return DetectorState.DISABLED, "auto-disabled: over the configured GPU/FPS budget"
        return self._detector.state()

    async def sample(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> list[Detection]:
        """One frame through the detector, subject to the budget guard. Returns `[]` whenever
        inactive/gated - callers never need to check `.active` separately."""
        if self._budget_disabled:
            self._maybe_recover()
        if not self.active:
            return []
        result = self._detector.detect(frame, arena_region=arena_region)
        self._recent_process_ms.append(result.process_time_ms)
        await self._check_budget(result.process_time_ms)
        gated = [d for d in result.detections if d.confidence >= self._cfg.min_confidence]
        if gated and self._emit is not None:
            await self._emit(
                "rl.vision.detections",
                {
                    "backend": self._detector.name,
                    "detections": [d.model_dump() for d in gated],
                },
            )
        return gated

    async def _check_budget(self, process_time_ms: float) -> None:
        interval_ms = (1000.0 / self._cfg.sample_hz) if self._cfg.sample_hz > 0 else 1000.0
        fraction = process_time_ms / interval_ms if interval_ms > 0 else 0.0
        if fraction > self._cfg.max_process_fraction:
            self._over_budget_streak += 1
        else:
            self._over_budget_streak = 0
        if (
            self._over_budget_streak >= self._cfg.consecutive_over_budget
            and not self._budget_disabled
        ):
            self._budget_disabled = True
            self._disabled_at = self._now()
            if self._emit is not None:
                await self._emit(
                    "rl.vision.disabled",
                    {
                        "reason": (
                            f"over budget: processing took {fraction:.0%} of the sample interval "
                            f"for {self._cfg.consecutive_over_budget} consecutive samples"
                        ),
                        "automatic": True,
                    },
                )

    def _maybe_recover(self) -> None:
        if self._disabled_at is None:
            return
        if self._now() - self._disabled_at >= self._cfg.cooldown_s:
            self._budget_disabled = False
            self._over_budget_streak = 0
            self._disabled_at = None
