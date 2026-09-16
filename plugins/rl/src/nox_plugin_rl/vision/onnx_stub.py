"""ONNX Runtime backend - documented extension point, deliberately inert today (Spec v0.9 §4.3,
SP-19's no-go on this machine: no PyTorch/ONNX runtime is installed and ENGINEERING.md's hard rule
is not to add a heavy ML dependency for this pass). `onnxruntime` is not in the `rl` extra
(`pyproject.toml` untouched by this story), so `state()` always reports `unavailable` and `detect()`
always returns zero detections - never a fabricated result (P10: "no fake detections ever").

To implement for real once a candidate model exists (future spike, not SP-19): add `onnxruntime` to
an extra, export the chosen model to ONNX, load it via `onnxruntime.InferenceSession(model_path)` in
`__init__`, run the arena crop through it in `detect()`, and map its class ids to `entity`/`team`.
"""

from __future__ import annotations

import numpy as np

from .interface import DetectionResult, DetectorState

try:
    import onnxruntime  # noqa: F401

    _ONNXRUNTIME_AVAILABLE = True
except ImportError:
    _ONNXRUNTIME_AVAILABLE = False


class OnnxDetector:
    name = "onnx"

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path

    def state(self) -> tuple[DetectorState, str]:
        if not _ONNXRUNTIME_AVAILABLE:
            return DetectorState.UNAVAILABLE, "onnxruntime not installed"
        if not self._model_path:
            return DetectorState.UNAVAILABLE, "no ONNX model configured"
        return DetectorState.UNAVAILABLE, "onnx backend not implemented (SP-19 no-go on this GPU)"

    def detect(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> DetectionResult:
        return DetectionResult(detections=[], process_time_ms=0.0)
