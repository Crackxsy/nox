"""Synthetic-frame tests for the classical OpenCV backend (ST-18-01/ST-18-02 AC1): drawn ball/car
blobs must be detected with roughly correct positions and non-trivial confidence; a blank field or
a degenerate arena region must never fabricate one (P10 "no fake detections ever")."""

from __future__ import annotations

import cv2
import numpy as np
from nox_plugin_rl.vision.interface import DetectorState
from nox_plugin_rl.vision.opencv_backend import OpenCvDetector

_W, _H = 480, 270


def _synthetic_frame(*, with_ball: bool = True, with_cars: bool = True) -> np.ndarray:
    frame = np.full((_H, _W, 3), (40, 120, 40), dtype=np.uint8)  # green "field"
    if with_ball:
        cv2.circle(frame, (_W // 2, _H // 2), 15, (235, 235, 235), -1)
    if with_cars:
        cv2.rectangle(frame, (60, 120), (100, 150), (200, 90, 20), -1)  # self: blue (BGR)
        cv2.rectangle(frame, (380, 120), (420, 150), (20, 120, 230), -1)  # opponent: orange (BGR)
    return frame


def test_state_is_available_when_cv2_installed() -> None:
    state, reason = OpenCvDetector().state()
    assert state == DetectorState.AVAILABLE
    assert reason


def test_detects_ball_and_both_team_cars_with_rough_positions() -> None:
    detector = OpenCvDetector()
    result = detector.detect(_synthetic_frame(), arena_region=(0.0, 0.0, 1.0, 1.0))

    entities = {d.entity for d in result.detections}
    assert "ball" in entities
    assert "car" in entities
    teams = {d.team for d in result.detections if d.entity == "car"}
    assert teams == {"self", "opponent"}

    ball = next(d for d in result.detections if d.entity == "ball")
    assert 0.35 < ball.x + ball.w / 2 < 0.65  # roughly centred
    assert 0.35 < ball.y + ball.h / 2 < 0.65
    assert ball.confidence > 0.2

    for car in (d for d in result.detections if d.entity == "car"):
        assert car.confidence > 0.25
        assert 0.0 <= car.x <= 1.0
        assert 0.0 <= car.y <= 1.0

    assert result.process_time_ms >= 0.0


def test_self_car_is_left_of_opponent_car_in_this_fixture() -> None:
    detector = OpenCvDetector()
    result = detector.detect(_synthetic_frame(), arena_region=(0.0, 0.0, 1.0, 1.0))
    self_car = next(d for d in result.detections if d.entity == "car" and d.team == "self")
    opponent_car = next(d for d in result.detections if d.entity == "car" and d.team == "opponent")
    assert self_car.x < opponent_car.x


def test_empty_field_yields_no_detections() -> None:
    detector = OpenCvDetector()
    frame = np.full((_H, _W, 3), (40, 120, 40), dtype=np.uint8)
    result = detector.detect(frame, arena_region=(0.0, 0.0, 1.0, 1.0))
    assert result.detections == []


def test_detect_never_raises_on_a_degenerate_arena_region() -> None:
    detector = OpenCvDetector()
    frame = _synthetic_frame()
    result = detector.detect(frame, arena_region=(0.99, 0.99, 0.001, 0.001))
    assert result.detections == []  # crop is empty/tiny - no crash, no fake detection
