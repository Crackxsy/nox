"""HUD calibration (ST-12-04): one `mss` screenshot, fixed percentage-based 1920x1080 default
region layout, self-check via the exact same recognizers ST-12-05 uses at runtime (Spec §3.5 -
"no separate calibration-only recognizer"). Honest `calibrated`/`uncalibrated` state: perception
never activates from unverified regions (A129, Spec §11)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import recognizers

# Percentage-based regions (left%, top%, width%, height% of the captured frame), calibrated
# against a 1920x1080 borderless reference layout (Spec §3.5) - scales to other resolutions since
# the values are fractions, not pixels. Placeholder ratios pending ES-04's real screenshot set;
# flagged in the ST-12-04 report, not silently presented as validated against the real HUD.
DEFAULT_REGIONS: dict[str, tuple[float, float, float, float]] = {
    "boost": (0.02, 0.90, 0.06, 0.06),
    "score_self": (0.44, 0.02, 0.04, 0.05),
    "score_opponent": (0.52, 0.02, 0.04, 0.05),
    "clock": (0.47, 0.02, 0.06, 0.05),
}


@dataclass(slots=True)
class RegionResult:
    ok: bool
    reason: str = ""


@dataclass(slots=True)
class CalibrationResult:
    calibrated: bool
    regions: dict[str, tuple[float, float, float, float]]
    failures: dict[str, str]  # region -> reason (ST-12-04 AC: name the specific failing region)
    calibrated_at: float
    screenshot_path: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CalibrationResult:
        return cls(
            calibrated=bool(data.get("calibrated", False)),
            regions={k: tuple(v) for k, v in data.get("regions", {}).items()},
            failures=dict(data.get("failures", {})),
            calibrated_at=float(data.get("calibrated_at", 0.0)),
            screenshot_path=str(data.get("screenshot_path", "")),
        )


def crop_region(frame: np.ndarray, region: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    left, top, rw, rh = region
    x0, y0 = int(left * w), int(top * h)
    x1, y1 = int((left + rw) * w), int((top + rh) * h)
    return frame[y0:y1, x0:x1]


def has_hud(frame: np.ndarray) -> bool:
    """Cheap "is this even a match HUD" gate (ST-12-04 AC: the main menu must fail with a clear
    "not in a match" reason, not a misleading per-region failure). A completely blank/uniform
    capture (main menu background, black screen) never looks like a HUD."""
    return bool(np.std(frame) > 3.0)


def self_check(
    frame: np.ndarray,
    templates: dict[str, np.ndarray],
    regions: dict[str, tuple[float, float, float, float]],
) -> dict[str, RegionResult]:
    results: dict[str, RegionResult] = {}
    boost_crop = crop_region(frame, regions["boost"])
    boost = recognizers.parse_boost(boost_crop, templates)
    results["boost"] = (
        RegionResult(True)
        if boost is not None
        else RegionResult(False, "boost did not parse as 0-100")
    )

    for key in ("score_self", "score_opponent"):
        crop = crop_region(frame, regions[key])
        score = recognizers.parse_score(crop, templates)
        results[key] = (
            RegionResult(True)
            if score is not None
            else RegionResult(False, f"{key} did not parse as 0-99")
        )

    clock_crop = crop_region(frame, regions["clock"])
    clock = recognizers.parse_clock(clock_crop, templates)
    results["clock"] = (
        RegionResult(True)
        if clock is not None
        else RegionResult(False, "clock did not parse as M:SS")
    )
    return results


def calibrate_from_frame(
    frame: np.ndarray,
    *,
    regions: dict[str, tuple[float, float, float, float]] | None = None,
    screenshot_path: str = "",
) -> CalibrationResult:
    regions = regions or DEFAULT_REGIONS
    if not has_hud(frame):
        return CalibrationResult(
            calibrated=False,
            regions=regions,
            failures={"hud": "no in-match HUD detected (main menu?) - not in a match"},
            calibrated_at=time.time(),
            screenshot_path=screenshot_path,
        )
    templates = recognizers.render_digit_templates()
    results = self_check(frame, templates, regions)
    failures = {k: r.reason for k, r in results.items() if not r.ok}
    return CalibrationResult(
        calibrated=not failures,
        regions=regions,
        failures=failures,
        calibrated_at=time.time(),
        screenshot_path=screenshot_path,
    )


def capture_frame() -> np.ndarray:
    """One `mss` screenshot of the primary monitor (Spec §3.5: "captures one screenshot")."""
    import mss  # local import: only needed on the machine actually running RL

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        arr = np.array(shot)  # BGRA
        return arr[:, :, :3]


def calibrate(
    *, state_path: Path, capture: object | None = None, save_screenshot: bool = True
) -> CalibrationResult:
    """`nox rl calibrate` entry point: capture, self-check, persist state (overwrites any previous
    calibration - ST-12-04 AC: recalibration replaces, never accumulates)."""
    frame = (capture or capture_frame)()  # type: ignore[operator]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path = ""
    if save_screenshot:
        try:
            import cv2

            screenshot_path = str(state_path.with_name("calibration_screenshot.png"))
            cv2.imwrite(screenshot_path, frame)
        except Exception:  # noqa: BLE001 - the screenshot is a nice-to-have, never blocks calibration
            screenshot_path = ""
    result = calibrate_from_frame(frame, screenshot_path=screenshot_path)
    state_path.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")
    return result


def load_calibration(state_path: Path) -> CalibrationResult | None:
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return CalibrationResult.from_json(data)
