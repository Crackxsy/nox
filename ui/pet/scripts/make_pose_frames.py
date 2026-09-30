"""Turns a cut-out pose picture into a rig pose frame.

Two jobs, both of which have to be right or the cross-dissolve looks like a glitch rather than a
move:

* **Scale.** A pose is drawn on the same quad as `base.png`, so it has to be the same size. The
  downscale averages in premultiplied alpha - dividing the colour back out afterwards - because
  averaging raw RGB across a transparent edge drags the colour of nothing into the silhouette and
  leaves a pale halo around the fur.
* **The ground line.** The cut-outs are centred on their canvas, so a lying creature would float in
  the middle of the frame while the standing one has its feet at the bottom. Each pose is shifted
  down until its lowest opaque pixel meets the base drawing's, which is what makes the dissolve read
  as the creature lying down *where it was standing*.

Run it from this directory:

    python make_pose_frames.py <cut-out.png> <name>

and it writes `../public/variants/meereswolf/rig/pose_<name>.png`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from pngio import read_rgba, write_rgba

VARIANT = Path(__file__).resolve().parent.parent / "public" / "variants" / "meereswolf" / "rig"
#: Anything below this is edge feathering, not silhouette. Used only to find the ground line.
OPAQUE = 8


def ground_line(image: np.ndarray) -> int:
    """Row of the lowest pixel that is actually part of the creature."""
    rows = np.nonzero((image[..., 3] > OPAQUE).any(axis=1))[0]
    if len(rows) == 0:
        raise SystemExit("that picture is empty")
    return int(rows[-1])


def halve(image: np.ndarray) -> np.ndarray:
    """Exact 2x downscale, averaged in premultiplied alpha so the silhouette keeps its colour."""
    height, width = image.shape[:2]
    if height % 2 or width % 2:
        raise SystemExit(f"{width}x{height} does not halve cleanly")
    colour = image[..., :3].astype(np.float64)
    alpha = image[..., 3:].astype(np.float64) / 255.0
    premultiplied = (colour * alpha).reshape(height // 2, 2, width // 2, 2, 3).mean(axis=(1, 3))
    alpha_small = alpha.reshape(height // 2, 2, width // 2, 2, 1).mean(axis=(1, 3))
    recovered = np.divide(
        premultiplied, alpha_small, out=np.zeros_like(premultiplied), where=alpha_small > 1e-6
    )
    return np.concatenate(
        [np.clip(recovered, 0, 255), np.clip(alpha_small * 255.0, 0, 255)], axis=-1
    ).astype(np.uint8)


def align_to(image: np.ndarray, target_row: int) -> np.ndarray:
    """Shift the picture vertically until its ground line sits on `target_row`."""
    shift = target_row - ground_line(image)
    if shift == 0:
        return image
    out = np.zeros_like(image)
    if shift > 0:
        out[shift:] = image[: image.shape[0] - shift]
    else:
        out[: image.shape[0] + shift] = image[-shift:]
    return out


def main(source: Path, name: str) -> None:
    base = read_rgba(VARIANT / "base.png")
    picture = read_rgba(source)
    while picture.shape[0] > base.shape[0]:
        picture = halve(picture)
    if picture.shape[:2] != base.shape[:2]:
        raise SystemExit(f"{picture.shape[1]}x{picture.shape[0]} does not match the base drawing")

    target = ground_line(base)
    aligned = align_to(picture, target)
    destination = VARIANT / f"pose_{name}.png"
    write_rgba(destination, aligned)
    print(f"{source.name} -> {destination.name}: ground line {ground_line(picture)} -> {target}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_pose_frames.py <cut-out.png> <name>")
    main(Path(sys.argv[1]), sys.argv[2])
