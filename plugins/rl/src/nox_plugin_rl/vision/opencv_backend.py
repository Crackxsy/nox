"""Classical OpenCV backend (no-go on VLM/ONNX candidates for this GPU):
colour/shape heuristics only, no ML model, no GPU inference. Ball: a bright, low-saturation blob
found via Hough circle detection. Cars: saturated blue/orange team-colour blobs (Rocket League's
default team colours) via contour bounding boxes. Both run only inside the calibrated arena region
(downstream of the shared capture pipeline, never a second capture mechanism). Confidence is
deliberately conservative - a crude "how much of its own bounding box the colour mask actually
covers" fill ratio, not a calibrated probability; measured this to have real false-positive risk on
synthetic frames, so callers must not treat it as more certain than it is."""

from __future__ import annotations

import time

import numpy as np

from .interface import Detection, DetectionResult, DetectorState

try:
    import cv2

    _CV2_AVAILABLE = True
except ImportError:  # pragma: no cover - opencv-python-headless is in the `rl` extra
    _CV2_AVAILABLE = False

# HSV ranges tuned for Rocket League's default blue/orange team colours and a bright, near-white,
# low-saturation ball under standard arena lighting - approximate, not calibrated against real
# captures (same honesty pattern as `calibration.py`'s `DEFAULT_REGIONS`; flagged in).
_BALL_LOWER = np.array([0, 0, 200], dtype=np.uint8)
_BALL_UPPER = np.array([180, 60, 255], dtype=np.uint8)
_BLUE_LOWER = np.array([95, 80, 60], dtype=np.uint8)
_BLUE_UPPER = np.array([130, 255, 255], dtype=np.uint8)
_ORANGE_LOWER = np.array([5, 100, 100], dtype=np.uint8)
_ORANGE_UPPER = np.array([20, 255, 255], dtype=np.uint8)

_MIN_CAR_AREA_FRAC = 0.001  # ignore small colour noise
_MAX_CAR_AREA_FRAC = 0.20  # ignore near-full-frame false matches (HUD colour bleed, menus)
_BALL_MIN_CONFIDENCE = 0.2
_CAR_MIN_CONFIDENCE = 0.25


class OpenCvDetector:
    name = "opencv"

    def state(self) -> tuple[DetectorState, str]:
        if not _CV2_AVAILABLE:
            return DetectorState.UNAVAILABLE, "opencv-python-headless not installed"
        return DetectorState.AVAILABLE, "classical colour and shape heuristics"

    def detect(
        self, frame: np.ndarray, *, arena_region: tuple[float, float, float, float]
    ) -> DetectionResult:
        if not _CV2_AVAILABLE:
            return DetectionResult(detections=[], process_time_ms=0.0)
        t0 = time.perf_counter()
        try:
            detections = self._detect(frame, arena_region)
        except Exception:  # noqa: BLE001 - a detector must never crash the sampler/plugin
            detections = []
        return DetectionResult(
            detections=detections, process_time_ms=(time.perf_counter() - t0) * 1000.0
        )

    def _detect(
        self, frame: np.ndarray, arena_region: tuple[float, float, float, float]
    ) -> list[Detection]:
        h, w = frame.shape[:2]
        left, top, rw, rh = arena_region
        x0, y0 = int(left * w), int(top * h)
        x1, y1 = int((left + rw) * w), int((top + rh) * h)
        crop = frame[y0:y1, x0:x1]
        if crop.size == 0:
            return []
        crop_bgr = np.ascontiguousarray(crop[:, :, :3] if crop.shape[2] >= 3 else crop)
        hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
        ch, cw = crop_bgr.shape[:2]
        out: list[Detection] = []
        out.extend(self._find_ball(crop_bgr, hsv, cw, ch, x0=left, y0=top, rw=rw, rh=rh))
        out.extend(self._find_cars(hsv, cw, ch, x0=left, y0=top, rw=rw, rh=rh))
        return out

    def _find_ball(
        self,
        crop_bgr: np.ndarray,
        hsv: np.ndarray,
        cw: int,
        ch: int,
        *,
        x0: float,
        y0: float,
        rw: float,
        rh: float,
    ) -> list[Detection]:
        if cw < 4 or ch < 4:
            return []
        mask = cv2.inRange(hsv, _BALL_LOWER, _BALL_UPPER)
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.bitwise_and(gray, gray, mask=mask)
        gray = cv2.GaussianBlur(gray, (9, 9), 2)
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.5,
            minDist=max(cw, ch),
            param1=100,
            param2=18,
            minRadius=max(2, min(cw, ch) // 60),
            maxRadius=max(3, min(cw, ch) // 6),
        )
        if circles is None:
            return []
        cx, cy, r = circles[0][0]
        x0i, y0i = max(0, int(cx - r)), max(0, int(cy - r))
        x1i, y1i = min(cw, int(cx + r)), min(ch, int(cy + r))
        patch = mask[y0i:y1i, x0i:x1i]
        # Crude confidence: how much of the circle's own bounding box the colour mask actually
        # covers - a real ball fills it near-completely, colour noise does not.
        fill = float(np.mean(patch > 0)) if patch.size else 0.0
        confidence = max(0.0, min(1.0, fill * 0.9))
        if confidence < _BALL_MIN_CONFIDENCE:
            return []
        return [
            Detection(
                entity="ball",
                confidence=confidence,
                x=max(0.0, x0 + (cx - r) / cw * rw),
                y=max(0.0, y0 + (cy - r) / ch * rh),
                w=min(1.0, (2 * r) / cw * rw),
                h=min(1.0, (2 * r) / ch * rh),
                team=None,
            )
        ]

    def _find_cars(
        self, hsv: np.ndarray, cw: int, ch: int, *, x0: float, y0: float, rw: float, rh: float
    ) -> list[Detection]:
        out: list[Detection] = []
        frame_area = cw * ch
        if frame_area == 0:
            return out
        kernel = np.ones((3, 3), np.uint8)
        for team, lower, upper in (
            ("self", _BLUE_LOWER, _BLUE_UPPER),
            ("opponent", _ORANGE_LOWER, _ORANGE_UPPER),
        ):
            mask = cv2.inRange(hsv, lower, upper)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                area = cv2.contourArea(contour)
                area_frac = area / frame_area
                if area_frac < _MIN_CAR_AREA_FRAC or area_frac > _MAX_CAR_AREA_FRAC:
                    continue
                bx, by, bw, bh = cv2.boundingRect(contour)
                if bh == 0 or bw / bh < 0.4:
                    continue  # rejects thin HUD colour slivers, not a real car silhouette
                fill = area / (bw * bh) if bw * bh else 0.0
                confidence = max(0.0, min(1.0, fill * 0.8))
                if confidence < _CAR_MIN_CONFIDENCE:
                    continue
                out.append(
                    Detection(
                        entity="car",
                        confidence=confidence,
                        x=max(0.0, x0 + bx / cw * rw),
                        y=max(0.0, y0 + by / ch * rh),
                        w=min(1.0, bw / cw * rw),
                        h=min(1.0, bh / ch * rh),
                        team=team,
                    )
                )
        return out
