"""Calibration self-check (ST-12-04): fixtures engineered to pass, and ones engineered to fail
each region individually (Spec §12.1 AC: name the specific failing region, not a generic
failure)."""

from __future__ import annotations

import numpy as np
from nox_plugin_rl import calibration, recognizers


def _blank_frame(w: int = 1920, h: int = 1080) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _paste(frame: np.ndarray, region: tuple[float, float, float, float], glyph: np.ndarray) -> None:
    h, w = frame.shape[:2]
    left, top, rw, rh = region
    x0, y0 = int(left * w), int(top * h)
    gh, gw = glyph.shape[:2]
    frame[y0 : y0 + gh, x0 : x0 + gw] = glyph[:, :, None]


def _good_frame() -> np.ndarray:
    frame = (np.random.default_rng(0).integers(0, 30, (1080, 1920, 3))).astype(np.uint8)
    _paste(frame, calibration.DEFAULT_REGIONS["boost"], recognizers.render_digits("72"))
    _paste(frame, calibration.DEFAULT_REGIONS["score_self"], recognizers.render_digits("3"))
    _paste(frame, calibration.DEFAULT_REGIONS["score_opponent"], recognizers.render_digits("1"))
    _paste(frame, calibration.DEFAULT_REGIONS["clock"], recognizers.render_clock("4", "12"))
    return frame


def test_calibration_passes_when_all_regions_read_plausibly() -> None:
    result = calibration.calibrate_from_frame(_good_frame())
    assert result.calibrated is True
    assert result.failures == {}


def test_calibration_names_the_specific_failing_region_boost() -> None:
    frame = _good_frame()
    # Corrupt only the boost region.
    left, top, rw, rh = calibration.DEFAULT_REGIONS["boost"]
    h, w = frame.shape[:2]
    x0, y0 = int(left * w), int(top * h)
    frame[y0 : y0 + 24, x0 : x0 + 60] = 0
    result = calibration.calibrate_from_frame(frame)
    assert result.calibrated is False
    assert "boost" in result.failures
    assert "clock" not in result.failures


def test_calibration_reports_not_in_a_match_for_a_blank_main_menu_like_frame() -> None:
    result = calibration.calibrate_from_frame(_blank_frame())
    assert result.calibrated is False
    assert "hud" in result.failures
    assert "not in a match" in result.failures["hud"]


def test_recalibration_overwrites_previous_state(tmp_path) -> None:
    state_path = tmp_path / "calibration.json"
    first = calibration.calibrate(
        state_path=state_path, capture=_blank_frame, save_screenshot=False
    )
    assert first.calibrated is False
    second = calibration.calibrate(
        state_path=state_path, capture=_good_frame, save_screenshot=False
    )
    assert second.calibrated is True
    loaded = calibration.load_calibration(state_path)
    assert loaded is not None
    assert loaded.calibrated is True
