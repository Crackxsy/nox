"""Cut the Eulenfuchs key art into animation parts for the Rig 2.0 and Relief spikes.

One painting in, a set of overlapping part textures out, so a bone can move a part without opening
a hole behind it. The method, per part (back to front):

* Every opaque pixel gets exactly one owner: the front-most part whose polygon contains it.
* A part fades out (Gaussian edge) only towards parts *behind* it. Towards parts in front of it, it
  stays opaque for a few pixels beyond the seam, so "front over back" reproduces the original at
  rest. The script measures that and prints the reconstruction error.
* Where a part continues behind a front part (the neck behind the head, the haunch behind the
  wing...) the hidden pixels are invented by inpainting from the part's own pixels, so a moving
  front part reveals plausible fur instead of the front part's ghost.
* The jaw is cut along the mouth line; behind it the head gets a painted mouth cavity.

It also writes a relief (height) map per part for the 2.5D spike, inflated from the part's own
silhouette plus a few hand-placed bumps (muzzle, nose, brow).

Run (dependencies come from uv, nothing is installed into the project):

    uv run --with numpy --with opencv-python-headless --with pillow python tools/cut_layers.py

Polygons are in pixels of the 512 x 512 key art, read off a gridded zoom of the picture.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
SPIKES = HERE.parent
SRC_DIR = SPIKES.parent / "public" / "variants" / "eulenfuchs" / "rig"
OUT = SPIKES / "public" / "eulenfuchs"
SIZE = 512

Poly = list[tuple[float, float]]


@dataclass
class Part:
    name: str
    poly: Poly | None  # None = everything nobody else claims
    feather: float = 1.2  # sigma (px) of the edge towards parts behind it
    underlay: list[Poly] = field(default_factory=list)  # regions continued behind front parts
    relief: float = 0.0  # height scale (in px of the 512 art) for the 2.5D spike


# Back to front.
PARTS: list[Part] = [
    Part("tail_l", [(80, 502), (82, 470), (98, 445), (124, 429), (150, 424), (172, 416), (190, 418),
                    (194, 445), (194, 475), (188, 505)],
         underlay=[[(176, 425), (206, 425), (206, 495), (176, 495)]], relief=22),
    Part("body", [(168, 296), (354, 296), (362, 330), (374, 400), (354, 418), (352, 507), (182, 507),
                  (182, 418), (154, 400), (158, 330)],
         feather=1.4,
         underlay=[
             [(196, 250), (324, 250), (334, 300), (318, 330), (202, 330), (186, 300)],  # neck
             [(172, 316), (198, 316), (198, 400), (174, 400)],  # flank behind left wing
             [(322, 316), (348, 316), (346, 400), (322, 400)],  # flank behind right wing
             [(338, 420), (360, 420), (362, 500), (338, 500)],  # haunch behind right tail
         ], relief=46),
    Part("tail_r", [(346, 420), (372, 414), (400, 420), (424, 440), (430, 470), (426, 505), (346, 505),
                    (342, 470)], relief=24),
    Part("wing_l", [(168, 310), (184, 314), (191, 335), (189, 362), (181, 386), (168, 399), (154, 397),
                    (152, 370), (155, 338), (159, 318)], relief=10),
    Part("wing_r", [(340, 310), (353, 316), (363, 334), (370, 356), (370, 380), (363, 398), (349, 400),
                    (339, 386), (333, 360), (333, 334)], relief=10),
    Part("head", [(148, 186), (160, 230), (163, 270), (169, 300), (185, 316), (215, 326), (260, 333),
                  (305, 326), (336, 316), (351, 300), (357, 270), (360, 230), (372, 186), (365, 110),
                  (150, 110)],
         feather=2.4,
         underlay=[[(172, 190), (226, 175), (228, 212), (176, 214)],
                   [(292, 175), (348, 190), (344, 214), (292, 212)]],
         relief=58),
    Part("ear_l", [(148, 118), (186, 118), (226, 162), (226, 190), (208, 199), (186, 205), (160, 206),
                   (148, 180)], feather=1.5, relief=8),
    Part("ear_r", [(292, 162), (332, 118), (372, 118), (372, 180), (360, 206), (336, 205), (314, 199),
                   (294, 190)], feather=1.5, relief=8),
    Part("jaw", [(232, 300), (246, 302.5), (256, 304.2), (266, 303.5), (278, 301.5), (290, 299),
                 (292, 306), (284, 315), (268, 320), (250, 320), (236, 314), (230, 306)],
         feather=0.9, relief=14),
]

# Eyes already exist as their own textures (the base has an eyeless facial disc under them).
EYES = {
    "eye_l": (0.38867, 0.42188),
    "eye_r": (0.50781, 0.41211),
}


def poly_mask(poly: Poly) -> np.ndarray:
    m = np.zeros((SIZE, SIZE), np.uint8)
    pts = np.round(np.array(poly, np.float64) * 8).astype(np.int32)
    cv2.fillPoly(m, [pts], 255, lineType=cv2.LINE_AA, shift=3)
    return m.astype(np.float32) / 255.0


def blur(m: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return m
    return cv2.GaussianBlur(m, (0, 0), sigma, borderType=cv2.BORDER_CONSTANT)


def dilate(m: np.ndarray, px: int) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(m, k)


def inpaint_from(rgb: np.ndarray, known: np.ndarray, region: np.ndarray, seed: int) -> np.ndarray:
    """Fill `region` from the pixels in `known` only, then add a little fur-scale noise."""
    img = (rgb * 255).clip(0, 255).astype(np.uint8)
    unknown = ((known < 0.5) & (region > 0.5)).astype(np.uint8)
    # Telea only uses pixels outside the mask, so everything that is neither known nor wanted is
    # masked too (and then ignored); that keeps other parts' colours out of the fill.
    mask = (known < 0.5).astype(np.uint8) * 255
    filled = cv2.inpaint(img, mask, 6, cv2.INPAINT_TELEA).astype(np.float32) / 255.0
    # Smooth the streaks Telea leaves, then add low-amplitude luminance grain so the fill does not
    # read as a flat airbrushed patch when it is revealed.
    smooth = cv2.GaussianBlur(filled, (0, 0), 2.0)
    rng = np.random.default_rng(seed)
    grain = cv2.GaussianBlur(rng.normal(0, 1, (SIZE, SIZE)).astype(np.float32), (0, 0), 0.8)
    grain = cv2.resize(cv2.resize(grain, (SIZE, SIZE // 3)), (SIZE, SIZE))  # vertical streaks = fur
    out = smooth * (1.0 + 0.10 * grain[..., None])
    return np.where(unknown[..., None] > 0, out, rgb)


def main() -> int:
    base = np.asarray(Image.open(SRC_DIR / "base.png").convert("RGBA"), np.float32) / 255.0
    # The key art is cropped at the bottom edge, so paws and tail end in a ruler-straight line.
    # On the ground the contact shadow hides it; in mid-hop it shows. Feather the last rows.
    rows = np.arange(SIZE, dtype=np.float32)[:, None]
    base[..., 3] *= np.clip((SIZE - 1 - rows) / 9.0, 0, 1) ** 0.8
    rgb, alpha = base[..., :3], base[..., 3]
    opaque = alpha > 0.002

    # Owner map: front-most polygon wins; unclaimed opaque pixels go to the nearest owner.
    owner = np.full((SIZE, SIZE), -1, np.int32)
    body_idx = next(i for i, p in enumerate(PARTS) if p.name == "body")
    for i, p in enumerate(PARTS):
        if p.poly is not None:
            owner[(poly_mask(p.poly) > 0.5) & opaque] = i
    stray = opaque & (owner < 0)
    owner[stray] = body_idx

    OUT.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"source": "ui/pet/public/variants/eulenfuchs/rig/base.png", "size": SIZE,
                      "parts": [], "eyes": []}
    layers: list[tuple[np.ndarray, np.ndarray]] = []
    names = [p.name for p in PARTS]

    # Pass 1: where each part *wants* to be opaque (w) and where it continues hidden (under).
    weights, unders, keeps = [], [], []
    for i, p in enumerate(PARTS):
        own = (owner == i).astype(np.float32)
        front = (owner > i).astype(np.float32)
        # Opaque into front parts for 3 sigma of *their* feather; soft towards parts behind.
        reach = int(np.ceil(3.2 * max([q.feather for q in PARTS[i + 1:]] + [1.0])))
        band = np.minimum(dilate(own, reach), front)
        extent = np.maximum(own, band)
        # Never feather against empty space: the painted silhouette already carries its own matte.
        extent = np.maximum(extent, np.minimum(dilate(own, 8), (~opaque).astype(np.float32)))
        w = np.maximum(blur(extent, p.feather), band)
        under = np.zeros_like(own)
        for poly in p.underlay:
            under = np.maximum(under, poly_mask(poly))
        if p.name == "head":  # the mouth cavity lives behind the jaw
            under = np.maximum(under, (owner == names.index("jaw")).astype(np.float32))
        weights.append(w.clip(0, 1))
        unders.append(blur(under, 2.5) * front)  # soft: a revealed continuation must not show a cut
        keeps.append(np.maximum(own, band))

    # Pass 2, front to back: each part takes exactly what is still missing to reproduce the art's
    # alpha, a = w * (alpha - F) / (1 - F), so the rest pose is exact by construction. Hidden
    # continuations are only opaque where the parts in front already cover fully (F ~ 1).
    alphas: list[np.ndarray] = [None] * len(PARTS)  # type: ignore[list-item]
    cover = np.zeros_like(alpha)
    for i in reversed(range(len(PARTS))):
        rest = np.where(cover < 0.999, (alpha - cover) / np.maximum(1 - cover, 1e-6), 0).clip(0, 1)
        a = weights[i] * rest
        hide = ((cover - 0.97) / 0.03).clip(0, 1)
        a = np.maximum(a, unders[i] * hide)
        alphas[i] = a
        cover = cover + (1 - cover) * a

    for i, p in enumerate(PARTS):
        a = alphas[i]
        own = (owner == i).astype(np.float32)
        # Colours: original within the seam band, inpainted further inside the hidden region.
        hidden = ((a > 0.002) & (keeps[i] < 0.5)).astype(np.float32)
        col = rgb.copy()
        if hidden.any():
            col = inpaint_from(rgb, own, hidden, seed=i)
        if p.name == "head":
            col = paint_mouth_cavity(col, owner, PARTS)
        layers.append((col, a))

        ys, xs = np.nonzero(a > 0.004)
        x0, y0 = max(0, xs.min() - 2), max(0, ys.min() - 2)
        x1, y1 = min(SIZE, xs.max() + 3), min(SIZE, ys.max() + 3)
        tile = np.dstack([col, a])[y0:y1, x0:x1]
        Image.fromarray((tile * 255 + 0.5).clip(0, 255).astype(np.uint8), "RGBA").save(
            OUT / f"{p.name}.png", optimize=True)
        depth = relief_map(p, a)[y0:y1, x0:x1]
        Image.fromarray((depth * 255 + 0.5).clip(0, 255).astype(np.uint8), "L").save(
            OUT / f"{p.name}_depth.png", optimize=True)
        manifest["parts"].append({"name": p.name, "file": f"{p.name}.png", "depth": f"{p.name}_depth.png",
                                  "rect": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
                                  "relief": p.relief})

    for name, (u, v) in EYES.items():
        im = Image.open(SRC_DIR / f"{name}.png").convert("RGBA")
        im.save(OUT / f"{name}.png", optimize=True)
        closed_eye(im, EYE_ELLIPSE[name]).save(OUT / f"{name}_closed.png", optimize=True)
        manifest["eyes"].append({"name": name, "file": f"{name}.png", "closed": f"{name}_closed.png",
                                 "rect": [round(u * SIZE), round(v * SIZE), im.width, im.height]})

    (OUT / "parts.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    report(base, layers)
    return 0


# Eye ellipse (ball + dark rim) in uv of each eye texture; keep in sync with shared/parts.ts.
EYE_ELLIPSE = {"eye_l": ((0.49, 0.53), (0.28, 0.265)), "eye_r": ((0.556, 0.57), (0.285, 0.272))}


def closed_eye(im: Image.Image, ellipse) -> Image.Image:
    """The lid skin: the eye patch with the ball inpainted from its own surrounding fur."""
    a = np.asarray(im, np.uint8)
    h, w = a.shape[:2]
    (cu, cv), (ru, rv) = ellipse
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.hypot((xx + 0.5) / w - cu, 0) ** 2 / ru ** 2 + ((yy + 0.5) / h - cv) ** 2 / rv ** 2
    mask = (d < 1.3 ** 2).astype(np.uint8) * 255
    rgb = cv2.inpaint(np.ascontiguousarray(a[..., :3]), mask, 5, cv2.INPAINT_TELEA).astype(np.float32)
    # fur grain so the lid is not airbrush-smooth: luminance noise streaked along the fur direction
    rng = np.random.default_rng(3)
    g = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 0.7)
    g = cv2.resize(cv2.resize(g, (w, max(2, h // 3))), (w, h))
    m = cv2.GaussianBlur(mask.astype(np.float32) / 255, (0, 0), 1.5)[..., None]
    rgb = rgb * (1 + 0.07 * g[..., None] * m)
    out = np.dstack([rgb.clip(0, 255).astype(np.uint8), a[..., 3]])
    return Image.fromarray(out, "RGBA")


def paint_mouth_cavity(col, owner, parts):
    """Under the jaw the head shows a mouth: dark gums, a hint of tongue, never the chin twice."""
    jaw_i = next(i for i, p in enumerate(parts) if p.name == "jaw")
    jaw = (owner == jaw_i).astype(np.float32)
    core = cv2.erode(jaw, np.ones((7, 7), np.uint8))  # 3 px in: jaw is opaque there
    ys, xs = np.nonzero(core)
    cy0, cy1 = ys.min(), ys.max()
    cx = xs.mean()
    yy, xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    t = ((yy - cy0) / max(1, cy1 - cy0)).clip(0, 1)
    throat = np.array([0.10, 0.035, 0.035], np.float32)
    tongue = np.array([0.55, 0.24, 0.24], np.float32)
    tongue_w = np.exp(-((xx - cx) / 14.0) ** 2) * t ** 1.4
    cavity = throat[None, None] * (1 - tongue_w[..., None]) + tongue[None, None] * tongue_w[..., None]
    w = blur(core, 0.8)[..., None]
    return col * (1 - w) + cavity * w


def relief_map(p: Part, a: np.ndarray) -> np.ndarray:
    """Height in [0,1] (x p.relief px): silhouette inflation plus a few anatomical bumps."""
    m = (a > 0.35).astype(np.uint8)
    if m.sum() == 0:
        return np.zeros_like(a)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    h = np.sqrt(dt / max(dt.max(), 1e-6))  # sqrt -> rounded "pillow" profile
    yy, xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)

    def bump(cx, cy, r, amp):
        return amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (r * r)))

    if p.name == "head":
        # Facial disc is a shallow dish; muzzle and nose stand proud of it.
        h = 0.62 * h - bump(232, 245, 26, 0.10) - bump(288, 242, 26, 0.10)
        h += bump(260, 292, 30, 0.34) + bump(261, 288, 11, 0.30)
    elif p.name == "jaw":
        h = 0.55 + 0.25 * h
    elif p.name == "body":
        h = 0.75 * h + bump(222, 470, 22, 0.18) + bump(288, 470, 22, 0.18) + bump(260, 360, 50, 0.22)
    h = blur(h.astype(np.float32), 2.0)
    return (h * (a > 0.002)).clip(0, 1)


def report(base: np.ndarray, layers) -> None:
    out = np.zeros((SIZE, SIZE, 4), np.float32)
    for col, a in layers:  # "over", straight alpha in, premultiplied accumulation
        out[..., :3] = col * a[..., None] + out[..., :3] * (1 - a[..., None])
        out[..., 3] = a + out[..., 3] * (1 - a)
    ref = base[..., :3] * base[..., 3:4]
    err = np.abs(out[..., :3] - ref)
    err_a = np.abs(out[..., 3] - base[..., 3])
    # The jaw-cavity pixels are covered by the jaw itself, so they do not show up here.
    mse = float((err ** 2).mean())
    psnr = 10 * np.log10(1.0 / max(mse, 1e-12))
    print(f"rest-pose reconstruction: PSNR {psnr:.1f} dB, max colour err {err.max():.3f}, "
          f"max alpha err {err_a.max():.3f}, pixels >2% off: {(err.max(-1) > 0.02).sum()}")


if __name__ == "__main__":
    sys.exit(main())
