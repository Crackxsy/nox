"""Cut the Meereswolf photo into the layers the deformation rig needs, and clean its matte.

The source (`idle_1024.png`) was keyed out of a JPEG that had no alpha at all, so its partially
transparent border still carries the dark background it was cut from: at alpha < 0.25 the fringe is
almost black. Composited on a light desktop that reads as black stubble around the creature. This
script therefore does two jobs, in one reproducible pass:

1. Matte repair - estimate the background colour the cut was made against, erode the matte by a
   pixel or two, unpremultiply the edge colour back to fur, re-feather, and *measure* the result.
2. Layer extraction - the eyes come out as their own small textures so a blink can be a real lid
   closing over an eyeball, and the socket underneath is inpainted with surrounding fur so the
   closed eye shows fur rather than a hole.

Run with the project venv (numpy only, no Pillow):

    .venv/Scripts/python.exe ui/pet/scripts/make_pet_layers.py

Writes into `ui/pet/public/variants/meereswolf/rig/` and prints a measurement table. Pass
`--report-only` to measure without writing. Landmarks are in `LANDMARKS_1024` and are stated in
source pixels so they can be re-read off the picture; everything else is derived.

Why not the muzzle as well: the wolf's mouth is shut in the photograph and there is no lip line to
cut along, so a "lower jaw" layer would expose invented pixels the moment it moved. The rig moves
the muzzle through the mesh instead, and a real speaking mouth needs new source art. See
`README.md` in this folder for what a better source must provide.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pngio import read_rgba, write_rgba  # noqa: E402  (needs the path above)

LOG = logging.getLogger("nox.pet." + Path(__file__).stem)

PET_DIR = Path(__file__).resolve().parents[1]
SOURCE = Path(r"E:\Nox\vault\16 - Assets\pet-art\meereswolf\idle_1024.png")
OUT_DIR = PET_DIR / "public" / "variants" / "meereswolf" / "rig"

#: Texture size written out. The pet window is 260 px, so 512 is a 2x reserve for HiDPI and keeps
#: the PNG under half a megabyte; 1024 would quadruple the upload for no visible gain.
TEXTURE_SIZE = 512
SOURCE_SIZE = 1024

#: Pixels of matte eroded before decontamination. One pixel removes the outermost, most polluted
#: ring; two starts eating real guard hair. Measured at 1024 px.
ERODE_PX = 2
#: Radius of the final feather, in source pixels.
FEATHER_PX = 1.2
#: Below this alpha the unpremultiplied colour is pure noise and the nearest fur colour is used.
COLOUR_TRUST_ALPHA = 0.18
#: An edge pixel may not end up darker than this fraction of the fur beside it. The source was cut
#: against a *checkerboard* of two greys, so no single background colour inverts the whole border;
#: this floor catches what is left over from the darker squares.
EDGE_DARKNESS_FLOOR = 0.72

#: Fur borrowed from this far around an eye when filling the socket behind it, and how many
#: push-pull passes that takes. Enough to reach the middle of the widest eye box.
SOCKET_FILL_MARGIN_PX = 56
SOCKET_FILL_ITERATIONS = 56
#: The push-pull weight decays geometrically away from known fur, so a small value is normal and
#: only the *ratio* colour/weight matters. This is the floor below which that ratio stops being
#: meaningful in float32 - reaching it means the fill never got there at all.
COVERAGE_MIN_WEIGHT = 1e-3
#: Transparent margin kept around an eye cut-out so its feathered rim is not clipped.
EYE_LAYER_PAD_PX = 8

#: Landmarks read off `idle_1024.png` at 4x zoom, in source pixels. Eye boxes are the visible
#: eyeball including its dark rim.
LANDMARKS_1024 = {
    "eye_l": {"box": (514, 230, 574, 281), "lid_y": 277},
    "eye_r": {"box": (658, 228, 712, 277), "lid_y": 273},
}


@dataclass(frozen=True)
class MatteReport:
    """What the matte looks like, before and after repair, in numbers a reviewer can check."""

    edge_pixels: int
    edge_luminance: float
    core_luminance: float
    darkest_decile_luminance: float
    #: Worst contrast any *border* pixel reaches against a light desktop. High means black stubble:
    #: a clean white-fur edge on #f5f5f7 stays close to 1:1.
    border_max_contrast_on_light: float
    #: Contrast of the mean body colour against a light desktop. A property of the photograph.
    body_contrast_on_light: float

    def as_row(self, label: str) -> str:
        return (
            f"{label:<7} edge_px={self.edge_pixels:6d}  edge_lum={self.edge_luminance:.3f}  "
            f"core_lum={self.core_luminance:.3f}  darkest_10%={self.darkest_decile_luminance:.3f}  "
            f"border_max={self.border_max_contrast_on_light:5.2f}:1  "
            f"body={self.body_contrast_on_light:.2f}:1"
        )


LIGHT_DESKTOP = np.array([0xF5 / 255, 0xF5 / 255, 0xF7 / 255], dtype=np.float32)


def _srgb_to_linear(channel: np.ndarray) -> np.ndarray:
    return np.where(channel <= 0.04045, channel / 12.92, ((channel + 0.055) / 1.055) ** 2.4)


def _relative_luminance(rgb: np.ndarray) -> np.ndarray:
    linear = _srgb_to_linear(rgb)
    return 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]


def _contrast(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (np.maximum(a, b) + 0.05) / (np.minimum(a, b) + 0.05)


def measure(rgb: np.ndarray, alpha: np.ndarray) -> MatteReport:
    """Describe a matte: how dark its border is and how it survives a light desktop."""
    edge = (alpha > 0.02) & (alpha < 0.98)
    core = alpha >= 0.98
    luminance = _relative_luminance(rgb)
    composited = rgb * alpha[..., None] + LIGHT_DESKTOP * (1 - alpha[..., None])
    contrast = _contrast(_relative_luminance(composited), _relative_luminance(LIGHT_DESKTOP))
    edge_luminance = luminance[edge]
    return MatteReport(
        edge_pixels=int(edge.sum()),
        edge_luminance=float(edge_luminance.mean()),
        core_luminance=float(luminance[core].mean()),
        darkest_decile_luminance=float(np.percentile(edge_luminance, 10)),
        border_max_contrast_on_light=float(np.percentile(contrast[edge], 99.9)),
        body_contrast_on_light=float(
            _contrast(
                _relative_luminance(rgb[core].mean(axis=0)), _relative_luminance(LIGHT_DESKTOP)
            )
        ),
    )


def _box_blur(field: np.ndarray, radius: int) -> np.ndarray:
    """Separable box blur with edge clamping. Enough for matte work; no scipy needed."""
    kernel_width = radius * 2 + 1
    trailing = [(0, 0)] * (field.ndim - 2)
    padded = np.pad(field, [(radius, radius), (radius, radius)] + trailing, mode="edge")
    accumulated = np.zeros_like(field, dtype=np.float32)
    for offset in range(kernel_width):
        accumulated += padded[offset : offset + field.shape[0], radius:-radius or None]
    horizontal = accumulated / kernel_width
    padded = np.pad(horizontal, [(0, 0), (radius, radius)] + trailing, mode="edge")
    accumulated = np.zeros_like(field, dtype=np.float32)
    for offset in range(kernel_width):
        accumulated += padded[:, offset : offset + field.shape[1]]
    return accumulated / kernel_width


def _erode(alpha: np.ndarray, pixels: int) -> np.ndarray:
    """Shrink the matte by taking the local minimum over a (2n+1)^2 window."""
    eroded = alpha
    for _ in range(pixels):
        stack = [eroded]
        for shift, axis in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            stack.append(np.roll(eroded, shift, axis=axis))
        eroded = np.minimum.reduce(stack)
    return eroded


def measure_background(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """The colour the cut was composited against, read off the fully transparent pixels.

    Measured, not fitted: whatever tool produced `idle_1024.png` stored *premultiplied* colour under
    a straight-alpha flag, which leaves this value at black and the whole border too dark by exactly
    the factor `alpha`.
    """
    return rgb[alpha <= 0.002].mean(axis=0)


class SocketFillError(RuntimeError):
    """The fur fill never reached the middle of a region it was asked to cover."""


def nearest_fur_colour(
    rgb: np.ndarray, confident: np.ndarray, iterations: int = 24, require_coverage: bool = False
) -> np.ndarray:
    """Flood the confident (opaque) colours outwards so every pixel has a plausible fur colour.

    Used twice: as the `fur` term when solving for the background, and as the colour of record for
    edge pixels too transparent to unpremultiply. A push-pull blur rather than a real inpaint -
    the result is only ever seen through a low alpha or behind a closed eyelid.
    """
    known = confident[..., None]
    weighted_colour = rgb * known
    weight = confident.astype(np.float32)
    for _ in range(iterations):
        weighted_colour = np.where(known, rgb, _box_blur(weighted_colour, 2))
        weight = np.where(confident, 1.0, _box_blur(weight, 2))
        if float(weight.min()) > 0.999:
            break
    if require_coverage and float(weight.min()) < COVERAGE_MIN_WEIGHT:
        raise SocketFillError(
            f"fur fill reached only weight {weight.min():.4f} in the worst pixel after "
            f"{iterations} passes - raise SOCKET_FILL_ITERATIONS or SOCKET_FILL_MARGIN_PX"
        )
    return np.clip(weighted_colour / np.maximum(weight, 1e-4)[..., None], 0, 1)


def clean_matte(rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Erode, decontaminate and re-feather. Returns (rgb, alpha, measured background colour).

    Order matters: erode first so the most polluted ring is gone before any colour is trusted,
    decontaminate against the measured background, then feather the *eroded* matte back out by
    about a pixel so the silhouette is not a hard cut.
    """
    fur = nearest_fur_colour(rgb, alpha >= 0.98)
    background = measure_background(rgb, alpha)

    eroded = _erode(alpha, ERODE_PX)
    feathered = np.clip(_box_blur(eroded, max(1, round(FEATHER_PX))), 0, 1).astype(np.float32)

    # Colour is recovered against the *original* alpha, which is what it was mixed with.
    safe_alpha = np.maximum(alpha, 1e-3)[..., None]
    unpremultiplied = np.clip((rgb - (1 - alpha)[..., None] * background) / safe_alpha, 0, 1)
    trusted = (alpha >= COLOUR_TRUST_ALPHA)[..., None]
    cleaned = np.where(trusted, unpremultiplied, fur)

    # What the checkerboard's darker squares left behind: edge pixels still far darker than the fur
    # beside them are lifted back towards it rather than shipped as stubble.
    fur_luminance = _relative_luminance(fur)
    cleaned_luminance = _relative_luminance(cleaned)
    too_dark = cleaned_luminance < fur_luminance * EDGE_DARKNESS_FLOOR
    rescue = (feathered > 0.001) & (feathered < 0.995) & too_dark
    cleaned = np.where(rescue[..., None], fur, cleaned)
    return cleaned.astype(np.float32), feathered, background


def _feathered_ellipse(
    shape: tuple[int, int], box: tuple[int, int, int, int], softness: float
) -> np.ndarray:
    """A soft-edged ellipse inscribed in `box`, as a 0..1 mask the size of the whole image."""
    left, top, right, bottom = box
    cx, cy = (left + right) / 2, (top + bottom) / 2
    rx, ry = (right - left) / 2, (bottom - top) / 2
    ys, xs = np.mgrid[0 : shape[0], 0 : shape[1]].astype(np.float32)
    radial = np.sqrt(((xs - cx) / rx) ** 2 + ((ys - cy) / ry) ** 2)
    return np.clip((1.0 + softness - radial) / softness, 0, 1).astype(np.float32)


def _resize(image: np.ndarray, size: int) -> np.ndarray:
    """Box-average downscale by an integer factor. The only resampling this pipeline needs."""
    factor = image.shape[0] // size
    if factor < 1 or image.shape[0] % size:
        raise ValueError(f"cannot box-downscale {image.shape[0]} to {size}")
    if factor == 1:
        return image
    height, width = size, size
    return image.reshape(height, factor, width, factor, image.shape[2]).mean(axis=(1, 3))


def _to_png(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return np.concatenate(
        [np.clip(rgb * 255 + 0.5, 0, 255), np.clip(alpha[..., None] * 255 + 0.5, 0, 255)], axis=2
    ).astype(np.uint8)


def build(write: bool) -> dict[str, object]:
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    source = read_rgba(SOURCE).astype(np.float32) / 255.0
    rgb, alpha = source[:, :, :3], source[:, :, 3]
    before = measure(rgb, alpha)

    cleaned_rgb, cleaned_alpha, background = clean_matte(rgb, alpha)
    after = measure(cleaned_rgb, cleaned_alpha)

    # Eye layers. The socket in the base texture is filled with surrounding fur so a closed lid
    # shows fur; the layer itself keeps the eyeball with a soft rim so it composites without a seam.
    socket_rgb = cleaned_rgb.copy()
    layers: dict[str, object] = {}
    for name, landmark in LANDMARKS_1024.items():
        left, top, right, bottom = landmark["box"]
        # The fill runs on a crop, not the whole picture: it converges in far fewer iterations and
        # can only ever borrow colour from the fur that actually surrounds this eye.
        margin = SOCKET_FILL_MARGIN_PX
        window = (left - margin, top - margin, right + margin, bottom + margin)
        patch = socket_rgb[window[1] : window[3], window[0] : window[2]]
        mask = _feathered_ellipse(
            patch.shape[:2],
            (margin, margin, margin + right - left, margin + bottom - top),
            softness=0.30,
        )
        filled = nearest_fur_colour(
            patch, mask < 0.02, iterations=SOCKET_FILL_ITERATIONS, require_coverage=True
        )
        socket_rgb[window[1] : window[3], window[0] : window[2]] = (
            patch * (1 - mask[..., None]) + filled * mask[..., None]
        )
        pad = EYE_LAYER_PAD_PX
        crop = (left - pad, top - pad, right + pad, bottom + pad)
        eye_rgb = cleaned_rgb[crop[1] : crop[3], crop[0] : crop[2]]
        eye_alpha = mask[
            crop[1] - window[1] : crop[3] - window[1], crop[0] - window[0] : crop[2] - window[0]
        ] * cleaned_alpha[crop[1] : crop[3], crop[0] : crop[2]]
        if write:
            write_rgba(OUT_DIR / f"{name}.png", _to_png(eye_rgb, eye_alpha))
        layers[name] = {
            "file": f"{name}.png",
            "rect": [round(v / SOURCE_SIZE, 5) for v in crop],
            "lidPivotY": round(landmark["lid_y"] / SOURCE_SIZE, 5),
        }

    if write:
        base = _resize(np.concatenate([socket_rgb, cleaned_alpha[..., None]], axis=2), TEXTURE_SIZE)
        write_rgba(OUT_DIR / "base.png", _to_png(base[:, :, :3], base[:, :, 3]))

    report = {
        "source": SOURCE.name,
        "measured_background_rgb": [round(float(c), 4) for c in background],
        "erode_px": ERODE_PX,
        "before": before.__dict__,
        "after": after.__dict__,
        "layers": layers,
    }
    if write:
        (OUT_DIR / "layers.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s", before.as_row("before"))
    LOG.info("%s", after.as_row("after"))
    LOG.info("measured background colour: %s", report["measured_background_rgb"])
    if after.body_contrast_on_light < 3.0:
        LOG.warning(
            "the body still sits below 3:1 against #f5f5f7. That is the photograph, not the "
            "matte - a light desktop needs a rim or a contact shadow, or a new source image."
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-only", action="store_true", help="measure without writing files")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if not SOURCE.exists():
        LOG.error("source image missing: %s", SOURCE)
        return 1
    build(write=not args.report_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
