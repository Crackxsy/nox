"""`NoneDetector` (always empty, zero cost) and the `onnx` backend stub (always `unavailable` in
this environment - no `onnxruntime` dependency, per ENGINEERING.md's hard rule not to add a heavy ML
dependency for this pass). Neither backend ever fabricates a detection (P10)."""

from __future__ import annotations

import numpy as np
from nox_plugin_rl.vision.interface import DetectorState
from nox_plugin_rl.vision.none_backend import NoneDetector
from nox_plugin_rl.vision.onnx_stub import OnnxDetector

_FRAME = np.zeros((10, 10, 3), dtype=np.uint8)
_REGION = (0.0, 0.0, 1.0, 1.0)


def test_none_detector_is_always_empty_and_zero_cost() -> None:
    result = NoneDetector().detect(_FRAME, arena_region=_REGION)
    assert result.detections == []
    assert result.process_time_ms == 0.0


def test_none_detector_state_is_disabled() -> None:
    state, reason = NoneDetector().state()
    assert state == DetectorState.DISABLED
    assert reason


def test_onnx_detector_reports_unavailable_without_a_configured_model() -> None:
    # Honest either way: whether `onnxruntime` happens to be importable in this environment or not,
    # a stub with no model configured must never claim to be `active` - only the specific reason
    # differs (missing dependency vs. missing model vs. "not implemented").
    state, reason = OnnxDetector().state()
    assert state == DetectorState.UNAVAILABLE
    assert reason in (
        "onnxruntime not installed",
        "no ONNX model configured",
    )


def test_onnx_detector_never_fabricates_a_detection_even_with_a_model_path_set() -> None:
    detector = OnnxDetector(model_path="does-not-matter.onnx")
    result = detector.detect(_FRAME, arena_region=_REGION)
    assert result.detections == []
    state, reason = detector.state()
    assert state == DetectorState.UNAVAILABLE
    assert reason  # honest, non-empty explanation either way
