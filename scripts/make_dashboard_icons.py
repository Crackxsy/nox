"""Make the dashboard's app icons (home screen, PWA) from the pet artwork.

    python scripts/make_dashboard_icons.py

Writes ui/dashboard/public/icon-192.png, icon-512.png and apple-touch-icon.png (180 px). The
creature is cut out by its alpha channel and centred inside the inner 70 % of a black square -
the safe zone a maskable icon keeps on every launcher shape - matching the dashboard's feature
tiles. Deterministic: the same artwork gives the same files.

Needs OpenCV and NumPy (`uv sync --extra rl`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "ui" / "pet" / "public" / "variants" / "eulenfuchs" / "idle.png"
OUT = ROOT / "ui" / "dashboard" / "public"
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}
SAFE = 0.70
BACKGROUND = (0, 0, 0)


def cut_out(image: np.ndarray) -> np.ndarray:
    alpha = image[:, :, 3]
    ys, xs = np.nonzero(alpha > 8)
    if len(xs) == 0:
        raise SystemExit(f"{SOURCE} has no visible pixels")
    return image[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]


def compose(creature: np.ndarray, size: int) -> np.ndarray:
    height, width = creature.shape[:2]
    scale = (size * SAFE) / max(height, width)
    resized = cv2.resize(
        creature, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA
    )
    canvas = np.zeros((size, size, 3), dtype=np.float32)
    canvas[:] = BACKGROUND
    h, w = resized.shape[:2]
    top, left = (size - h) // 2, (size - w) // 2
    rgb = resized[:, :, :3].astype(np.float32)
    alpha = resized[:, :, 3:4].astype(np.float32) / 255.0
    region = canvas[top : top + h, left : left + w]
    canvas[top : top + h, left : left + w] = rgb * alpha + region * (1.0 - alpha)
    return canvas.round().astype(np.uint8)


def main() -> int:
    image = cv2.imread(str(SOURCE), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        print(f"cannot read an RGBA image from {SOURCE}", file=sys.stderr)
        return 1
    creature = cut_out(image)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        cv2.imwrite(str(OUT / name), compose(creature, size), [cv2.IMWRITE_PNG_COMPRESSION, 9])
        print(f"wrote {(OUT / name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
