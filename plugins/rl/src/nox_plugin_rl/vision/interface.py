"""`VisionDetector` abstraction (ST-18-02, Spec v0.9 §4.3): a frame and a calibrated arena region
in, rough bounding-box detections with a confidence score out - never an exception (a backend that
fails reports `unavailable` via `state()`, it never raises out of `detect()` into the sampler/
plugin, and it never silently substitutes another backend while claiming to be the configured one -
P10). Positions are fractions of the full captured frame (0..1), matching `calibration.py`'s region
convention, so downstream code never needs to know the capture resolution.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

import numpy as np
from pydantic import BaseModel, Field

#: Rough default arena crop (excludes the top/bottom HUD strips) - a Stage-2-only constant, never
#: written into `calibration.DEFAULT_REGIONS` (ST-18-03 AC: Stage 2 reuses the capture pipeline
#: without modifying it). Percentage-based, same convention as `calibration.crop_region`.
DEFAULT_ARENA_REGION: tuple[float, float, float, float] = (0.03, 0.12, 0.94, 0.76)


class DetectorState(StrEnum):
    """Honest operational state (P10, Spec v0.9 §4.3/§9) - never "active" while actually running
    `NoneDetector` or a failed backend."""

    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class Detection(BaseModel):
    """One rough bounding-box detection (Spec §7 `vision_stage2_detections`)."""

    entity: str  # "ball" | "car"
    confidence: float = Field(ge=0.0, le=1.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)
    team: str | None = None  # "self" | "opponent" | None (car heuristic only)


class DetectionResult(BaseModel):
    detections: list[Detection] = Field(default_factory=list)
    #: The backend's own measured wall-clock cost for this call - the only budget signal available
    #: without a live GPU counter/PresentMon (SP-05 gates that); the sampler's budget guard uses
    #: this directly (see `sampler.py`).
    process_time_ms: float = 0.0


class VisionDetector(Protocol):
    """Every backend: frame in, detections out, never raises."""

    name: str

    def detect(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> DetectionResult: ...

    def state(self) -> tuple[DetectorState, str]: ...
