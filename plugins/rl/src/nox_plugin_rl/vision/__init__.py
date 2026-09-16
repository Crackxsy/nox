"""Vision Stage 2 (Spec v0.9 Vision Stage 2, EPIC-18, ST-18-01..06): a swappable `VisionDetector`
abstraction over rough ball/car position detection, a classical OpenCV backend, an always-empty
`NoneDetector`, a documented-but-inert `onnx` stub, and the `FrameSampler` budget guard that runs a
backend at a low rate downstream of the plugin's existing capture pipeline and disables itself when
its own measured processing cost crosses the configured budget. Observation only - no backend here
ever synthesizes input or reads game memory (Security Model §10; the CI guard greps this package
like the rest of `plugins/rl/**`).
"""

from __future__ import annotations

from .interface import (
    DEFAULT_ARENA_REGION,
    Detection,
    DetectionResult,
    DetectorState,
    VisionDetector,
)
from .none_backend import NoneDetector
from .onnx_stub import OnnxDetector
from .opencv_backend import OpenCvDetector
from .sampler import FrameSampler, VisionBudgetConfig

__all__ = [
    "DEFAULT_ARENA_REGION",
    "Detection",
    "DetectionResult",
    "DetectorState",
    "VisionDetector",
    "NoneDetector",
    "OnnxDetector",
    "OpenCvDetector",
    "FrameSampler",
    "VisionBudgetConfig",
    "build_detector",
]


def build_detector(backend: str, *, onnx_model_path: str | None = None) -> VisionDetector:
    """Backend selection (config `rl.vision.backend`). Never raises - an unknown/unavailable
    backend name falls back to `NoneDetector` rather than crashing the plugin (P10: the resulting
    `state()` still reports honestly, it just never reports `active` for a backend that isn't
    really running)."""
    if backend == "opencv":
        return OpenCvDetector()
    if backend == "onnx":
        return OnnxDetector(onnx_model_path)
    return NoneDetector()
