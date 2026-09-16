"""Always-empty, zero-cost detector (ST-18-02 AC: "always returns no detections and consumes no
GPU/measurable resources"). The safe default when Stage 2 is off (`rl.vision.backend: none`) or
when a real backend has not been selected."""

from __future__ import annotations

import numpy as np

from .interface import DetectionResult, DetectorState


class NoneDetector:
    name = "none"

    def detect(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> DetectionResult:
        return DetectionResult(detections=[], process_time_ms=0.0)

    def state(self) -> tuple[DetectorState, str]:
        return DetectorState.DISABLED, "none backend selected: Stage 2 vision is off"
