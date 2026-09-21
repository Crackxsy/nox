"""Stage-1 HUD recognizers: cropped-region template matching for boost/score/clock digits via
opencv -
no OCR/Tesseract dependency.

Real HUD pixel templates need ES-04 (the PO's screenshot set, Plan v0.3 - "not provided yet" as of
this release); this module ships a synthetic digit-template set rendered with `cv2.putText` on a
fixed monospace-style cell grid, so the matcher and every self-check/unit test are real and CI-safe
today. Both the templates and `render_digits`'s fixtures use the same per-cell renderer
(`_render_glyph`), so cell boundaries always line up exactly - real HUD digits are also fixed-width
in Rocket League's HUD font, so `read_digits`'s fixed-cell-grid approach carries over unchanged;
only the template source (`render_digit_templates` vs. a real-crop loader) needs to change once
ES-04 lands - flagged as an open point in the/05 report, not silently pretended to be validated
against the real game.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

DIGITS = "0123456789"
CELL = (24, 16)  # (height, width) px - one fixed-width glyph cell
_MATCH_THRESHOLD = 0.5  # cv2.TM_CCOEFF_NORMED score below this = "no confident match" (A129)


def _render_glyph(ch: str, *, font_scale: float = 0.7, thickness: int = 1) -> np.ndarray:
    h, w = CELL
    img = np.zeros((h, w), dtype=np.uint8)
    cv2.putText(
        img, ch, (2, h - 5), cv2.FONT_HERSHEY_SIMPLEX, font_scale, 255, thickness, cv2.LINE_AA
    )
    return img


def render_digit_templates(*, font_scale: float = 0.7, thickness: int = 1) -> dict[str, np.ndarray]:
    """One `CELL`-sized grayscale template per digit 0-9 (deterministic, no external asset)."""
    return {ch: _render_glyph(ch, font_scale=font_scale, thickness=thickness) for ch in DIGITS}


def render_digits(text: str, *, font_scale: float = 0.7, thickness: int = 1) -> np.ndarray:
    """Render `text` (digits only) as a row of `CELL`-sized glyph cells, for fixtures/self-tests -
    exactly the grid `read_digits` expects, so a template built the same way always matches."""
    h, w = CELL
    cells = [_render_glyph(ch, font_scale=font_scale, thickness=thickness) for ch in text]
    if not cells:
        return np.zeros((h, w), dtype=np.uint8)
    return np.concatenate(cells, axis=1)


@dataclass(frozen=True, slots=True)
class MatchResult:
    text: str
    confidence: float  # 0..1, min over all matched glyphs (silence-over-guessing needs the worst)


def read_digits(
    crop: np.ndarray, templates: dict[str, np.ndarray], *, max_digits: int = 3
) -> MatchResult | None:
    """Match each `CELL`-wide column of `crop` (left to right, up to `max_digits` cells) against
    the digit templates. Returns None the moment any cell is not a confident digit match (silence
    over guessing, A129) rather than returning a partially-guessed number."""
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h, w = CELL
    height, width = crop.shape[:2]
    if height < h or width < w:
        return None
    n_cells = min(max_digits, width // w)
    if n_cells == 0:
        return None
    digits: list[str] = []
    confidences: list[float] = []
    for i in range(n_cells):
        cell = crop[0:h, i * w : (i + 1) * w]
        best_ch, best_score = "", -1.0
        for ch, template in templates.items():
            score = float(cv2.matchTemplate(cell, template, cv2.TM_CCOEFF_NORMED)[0][0])
            if score > best_score:
                best_ch, best_score = ch, score
        if best_score < _MATCH_THRESHOLD:
            break
        digits.append(best_ch)
        confidences.append(best_score)
    if not digits:
        return None
    return MatchResult(text="".join(digits), confidence=min(confidences))


def parse_boost(crop: np.ndarray, templates: dict[str, np.ndarray]) -> tuple[int, float] | None:
    """Boost must read as an integer 0-100."""
    match = read_digits(crop, templates, max_digits=3)
    if match is None or not match.text.isdigit():
        return None
    value = int(match.text)
    if not (0 <= value <= 100):
        return None
    return value, match.confidence


def parse_score(crop: np.ndarray, templates: dict[str, np.ndarray]) -> tuple[int, float] | None:
    """A single small integer score (Spec: "two small integers", one crop per team here)."""
    match = read_digits(crop, templates, max_digits=2)
    if match is None or not match.text.isdigit():
        return None
    value = int(match.text)
    if not (0 <= value <= 99):
        return None
    return value, match.confidence


def parse_clock(
    crop: np.ndarray, templates: dict[str, np.ndarray], *, minute_digits: int = 1
) -> tuple[str, float] | None:
    """`M:SS` from a crop laid out as `minute_digits` cells + a 2-cell seconds group starting right
    after (the colon glyph between them is simply skipped, not matched). Overtime is banner-based,
    not handled here."""
    h, w = CELL
    gap = w  # one cell width reserved for the colon glyph, not matched
    minutes = read_digits(crop[:, : minute_digits * w], templates, max_digits=minute_digits)
    seconds_start = minute_digits * w + gap
    seconds = read_digits(crop[:, seconds_start:], templates, max_digits=2)
    if minutes is None or seconds is None:
        return None
    if not (minutes.text.isdigit() and seconds.text.isdigit() and len(seconds.text) == 2):
        return None
    sec_value = int(seconds.text)
    if not (0 <= sec_value <= 59):
        return None
    confidence = min(minutes.confidence, seconds.confidence)
    return f"{minutes.text}:{seconds.text}", confidence


def render_clock(minutes: str, seconds: str, *, font_scale: float = 0.7) -> np.ndarray:
    """Fixture helper: build a `parse_clock`-compatible crop (minute cell(s) + colon-gap + seconds
    cells)."""
    h, w = CELL
    gap_cell = np.zeros((h, w), dtype=np.uint8)
    cv2.putText(
        gap_cell, ":", (4, h - 5), cv2.FONT_HERSHEY_SIMPLEX, font_scale, 255, 1, cv2.LINE_AA
    )
    return np.concatenate(
        [
            render_digits(minutes, font_scale=font_scale),
            gap_cell,
            render_digits(seconds, font_scale=font_scale),
        ],
        axis=1,
    )
