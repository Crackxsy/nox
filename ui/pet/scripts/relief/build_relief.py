# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy==2.5.4", "opencv-python-headless==5.0.0.93", "pillow==13.0.0"]
# ///
"""Cut a creature's key art into the part layers of the relief (2.5D) renderer.

One painting in, everything the renderer needs out, into `public/variants/<id>/relief/`:

* `parts.png` - an atlas of overlapping part textures (body, head, ears, jaw, wings, tails...) plus
  the eye patches, open and closed. Parts overlap so a bone can move one without opening a hole.
* `depth.png` - a relief (height) map per part in the same atlas layout: the part's silhouette
  inflated into a rounded "pillow", plus a few hand-placed bumps (muzzle, nose, brow, haunches).
* `relief.json` - the rig: bones with spring settings, skin-weight rules, per-part depth, draw
  order, roles - read by `src/relief/reliefFile.ts`.

The cut, per part (back to front):

* Every opaque pixel gets exactly one owner: the front-most part whose polygon contains it.
* A part fades out (Gaussian edge) only towards parts *behind* it; towards parts in front it stays
  opaque a few pixels beyond the seam, so "front over back" reproduces the art at rest. The script
  measures that and prints the reconstruction error (PSNR).
* Where a part continues behind a front part (neck behind the head, flank behind a wing) the
  hidden pixels are inpainted from the part's own pixels, so a moving front part reveals fur, not a
  ghost of itself. Under the jaw the head gets a painted mouth cavity.
* The closed eye is the eye patch with the ball inpainted from its own fur: the lid skin.

Deterministic: fixed seeds, pinned dependencies, stable atlas packing, sorted JSON. The same
config and art give byte-identical output.

Run from the repository root (uv fetches the pinned dependencies; nothing is installed into the
project):

    uv run --no-project ui/pet/scripts/relief/build_relief.py eulenfuchs [--debug-dir DIR]
        [--enable]

`<creature>` is a config in `scripts/relief/creatures/<creature>.json`. A config may start from
another creature (`"template": "eulenfuchs"`) and only give landmarks and overrides; see
`scripts/README.md`, "Relief renderer: converting a creature".
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
PET = HERE.parent.parent
CREATURES = HERE / "creatures"
VARIANTS = PET / "public" / "variants"
PAD = 4  # px between atlas tiles: keeps mipmaps from bleeding one part into the next
ATLAS_WIDTH = 512
MIN_PSNR = 40.0  # below this the cut visibly changes the art at rest

Poly = list[list[float]]


# ---- config -------------------------------------------------------------------------------------


def load_config(name: str) -> dict[str, Any]:
    path = Path(name) if name.endswith(".json") else CREATURES / f"{name}.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if "template" in cfg:
        cfg = derive_from_template(load_config(cfg["template"]), cfg)
    return cfg


def fit_axes(src: list[list[float]], dst: list[list[float]]) -> tuple[float, float, float, float]:
    """Least-squares x' = sx x + tx, y' = sy y + ty (no rotation: every creature sits upright)."""
    s = np.asarray(src, np.float64)
    d = np.asarray(dst, np.float64)
    out = []
    for k in (0, 1):
        if np.ptp(s[:, k]) < 1e-6:
            out += [1.0, float((d[:, k] - s[:, k]).mean())]
            continue
        a, b = np.polyfit(s[:, k], d[:, k], 1)
        out += [float(a), float(b)]
    return out[0], out[2], out[1], out[3]  # sx, sy, tx, ty


class Warp:
    def __init__(self, sx: float, sy: float, tx: float, ty: float) -> None:
        self.sx, self.sy, self.tx, self.ty = sx, sy, tx, ty

    def x(self, v: float) -> float:
        return round(self.sx * v + self.tx, 1)

    def y(self, v: float) -> float:
        return round(self.sy * v + self.ty, 1)

    def pt(self, p: list[float]) -> list[float]:
        return [self.x(p[0]), self.y(p[1]), *p[2:]]

    def len(self, v: float) -> float:
        return round(v * float(np.sqrt(abs(self.sx * self.sy))), 2)


def warp_rule(rule: dict[str, Any], w: Warp) -> dict[str, Any]:
    r = copy.deepcopy(rule)
    kind = r["kind"]
    if kind == "chain":
        r["points"] = [w.pt(p) for p in r["points"]]
    elif kind == "ear":
        r["base"], r["tip"] = w.pt(r["base"]), w.pt(r["tip"])
    elif kind == "spine":
        r["ys"] = [w.y(v) for v in r["ys"]]
        for leg in r.get("legs", []):
            leg["x"] = w.x(leg["x"])
            leg["halfWidth"] = [round(v * w.sx, 1) for v in leg["halfWidth"]]
            leg["y"] = [w.y(v) for v in leg["y"]]
    elif kind == "head":
        r["neck"]["y"] = [w.y(v) for v in r["neck"]["y"]]
        if "cheeks" in r:
            c = r["cheeks"]
            c["centerX"] = w.x(c["centerX"])
            c["dx"] = [round(v * w.sx, 1) for v in c["dx"]]
            c["y"] = [w.y(v) for v in c["y"]]
    return r


def derive_from_template(tpl: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """A new creature from a converted one: every coordinate is moved by an axis-aligned fit of
    the template's landmarks onto the new creature's (globally, or per part with `fit`), then the
    new config's overrides and drops are applied. The result is a starting point to check on the
    overlay picture (`--debug-dir`), not a finished rig."""
    names = sorted(set(tpl["landmarks"]) & set(cfg["landmarks"]))
    if len(names) < 3:
        raise SystemExit("template and creature need at least 3 landmarks in common")

    def warp_for(subset: list[str] | None) -> Warp:
        use = [n for n in (subset or names) if n in tpl["landmarks"] and n in cfg["landmarks"]]
        if len(use) < 2:
            raise SystemExit(f"fit {subset}: needs two landmarks known to both configs")
        return Warp(
            *fit_axes([tpl["landmarks"][n] for n in use], [cfg["landmarks"][n] for n in use])
        )

    g = warp_for(None)
    out = copy.deepcopy(tpl)
    out.pop("template", None)
    for key in ("id", "source", "landmarks", "eyes", "comment"):
        if key in cfg:
            out[key] = cfg[key]
    out["ground"] = g.pt(tpl["ground"])
    out["artHeight"] = round(tpl["artHeight"] * g.sy, 1)
    overrides = cfg.get("parts", {})
    drop = set(cfg.get("drop", []))
    parts = []
    for p in tpl["parts"]:
        if p["name"] in drop:
            continue
        w = (
            warp_for(overrides.get(p["name"], {}).get("fit"))
            if overrides.get(p["name"], {}).get("fit")
            else g
        )
        q = copy.deepcopy(p)
        q["poly"] = [w.pt(v) for v in p["poly"]]
        q["underlay"] = [[w.pt(v) for v in poly] for poly in p.get("underlay", [])]
        if "depth" in q and "bumps" in q["depth"]:
            q["depth"]["bumps"] = [
                [w.x(b[0]), w.y(b[1]), w.len(b[2]), b[3]] for b in q["depth"]["bumps"]
            ]
        q["weights"] = warp_rule(p["weights"], w)
        q["relief"] = w.len(p["relief"])
        q.update({k: v for k, v in overrides.get(p["name"], {}).items() if k != "fit"})
        parts.append(q)
    out["parts"] = parts
    out["order"] = [n for n in tpl["order"] if n not in drop]
    drop_bones = set(cfg.get("dropBones", []))
    bone_over = cfg.get("bones", {})
    out["bones"] = []
    for b in tpl["bones"]:
        if b["name"] in drop_bones:
            continue
        nb = {**b, "pivot": g.pt(b["pivot"]), "tip": g.pt(b["tip"])}
        nb.update(bone_over.get(b["name"], {}))
        out["bones"].append(nb)
    head = next((p for p in out["parts"] if p["name"] == "head"), None)
    hw = (
        warp_for(overrides.get("head", {}).get("fit"))
        if head and overrides.get("head", {}).get("fit")
        else g
    )
    out["headCenter"] = [*hw.pt(tpl["headCenter"][:2]), tpl["headCenter"][2]]
    out["lookAnchor"] = hw.pt(tpl["lookAnchor"])
    out["bodyCenter"] = [*g.pt(tpl["bodyCenter"][:2]), tpl["bodyCenter"][2]]
    out["headBones"] = [b for b in tpl["headBones"] if b not in drop_bones]
    roles = copy.deepcopy(tpl["roles"])
    roles["ears"] = {k: [b for b in v if b not in drop_bones] for k, v in roles["ears"].items()}
    if roles.get("wings") and drop_bones & set(roles["wings"].values()):
        roles["wings"] = None
    roles["tails"] = [[b for b in t if b not in drop_bones] for t in roles["tails"]]
    roles["tails"] = [t for t in roles["tails"] if t]
    roles["legs"] = [b for b in roles["legs"] if b not in drop_bones]
    out["roles"] = roles
    for key in (
        "turn",
        "light",
        "focal",
        "neck",
        "mouth",
        "bottomFeather",
        "roles",
        "order",
        "headBones",
        "poses",
    ):
        if key in cfg:
            out[key] = cfg[key]
    return out


# ---- image helpers ------------------------------------------------------------------------------


def poly_mask(poly: Poly, size: int) -> np.ndarray:
    m = np.zeros((size, size), np.uint8)
    pts = np.round(np.array(poly, np.float64) * 8).astype(np.int32)
    cv2.fillPoly(m, [pts], 255, lineType=cv2.LINE_AA, shift=3)
    return m.astype(np.float32) / 255.0


def blur(m: np.ndarray, sigma: float) -> np.ndarray:
    return m if sigma <= 0 else cv2.GaussianBlur(m, (0, 0), sigma, borderType=cv2.BORDER_CONSTANT)


def dilate(m: np.ndarray, px: int) -> np.ndarray:
    return cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1)))


def fur_grain(h: int, w: int, seed: int, sigma: float) -> np.ndarray:
    """Low-amplitude luminance grain streaked vertically, so a fill does not read as airbrush."""
    rng = np.random.default_rng(seed)
    g = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), sigma)
    return cv2.resize(cv2.resize(g, (w, max(2, h // 3))), (w, h))


def inpaint_from(rgb: np.ndarray, known: np.ndarray, region: np.ndarray, seed: int) -> np.ndarray:
    """Fill `region` from the pixels in `known` only (other parts' colours stay out of the fill)."""
    size = rgb.shape[0]
    img = (rgb * 255).clip(0, 255).astype(np.uint8)
    unknown = (known < 0.5) & (region > 0.5)
    mask = (known < 0.5).astype(np.uint8) * 255
    filled = cv2.inpaint(img, mask, 6, cv2.INPAINT_TELEA).astype(np.float32) / 255.0
    smooth = cv2.GaussianBlur(filled, (0, 0), 2.0)
    out = smooth * (1.0 + 0.10 * fur_grain(size, size, seed, 0.8)[..., None])
    return np.where(unknown[..., None], out, rgb)


def closed_eye(im: Image.Image, ellipse: dict[str, list[float]]) -> Image.Image:
    """The lid skin: the eye patch with the ball inpainted from its own surrounding fur."""
    a = np.asarray(im, np.uint8)
    h, w = a.shape[:2]
    (cu, cv), (ru, rv) = ellipse["c"], ellipse["r"]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = ((xx + 0.5) / w - cu) ** 2 / ru**2 + ((yy + 0.5) / h - cv) ** 2 / rv**2
    mask = (d < 1.3**2).astype(np.uint8) * 255
    rgb = cv2.inpaint(np.ascontiguousarray(a[..., :3]), mask, 5, cv2.INPAINT_TELEA).astype(
        np.float32
    )
    m = cv2.GaussianBlur(mask.astype(np.float32) / 255, (0, 0), 1.5)[..., None]
    rgb = rgb * (1 + 0.07 * fur_grain(h, w, 3, 0.7)[..., None] * m)
    return Image.fromarray(np.dstack([rgb.clip(0, 255).astype(np.uint8), a[..., 3]]), "RGBA")


def paint_mouth_cavity(col: np.ndarray, jaw_owned: np.ndarray) -> np.ndarray:
    """Under the jaw the head shows a mouth: dark gums and a hint of tongue, not the chin twice."""
    size = col.shape[0]
    core = cv2.erode(jaw_owned, np.ones((7, 7), np.uint8))
    ys, xs = np.nonzero(core)
    if len(ys) == 0:
        return col
    cy0, cy1, cx = ys.min(), ys.max(), xs.mean()
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    t = ((yy - cy0) / max(1, cy1 - cy0)).clip(0, 1)
    throat = np.array([0.10, 0.035, 0.035], np.float32)
    tongue = np.array([0.55, 0.24, 0.24], np.float32)
    tw = (np.exp(-(((xx - cx) / 14.0) ** 2)) * t**1.4)[..., None]
    cavity = throat * (1 - tw) + tongue * tw
    w = blur(core, 0.8)[..., None]
    return col * (1 - w) + cavity * w


def relief_map(part: dict[str, Any], a: np.ndarray) -> np.ndarray:
    """Height 0..1 (x part.relief px): silhouette inflation plus the config's bumps."""
    size = a.shape[0]
    m = (a > 0.35).astype(np.uint8)
    if m.sum() == 0:
        return np.zeros_like(a)
    dt = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    h = np.sqrt(dt / max(float(dt.max()), 1e-6))  # sqrt: a rounded "pillow" profile
    spec = part.get("depth", {})
    h = spec.get("offset", 0.0) + spec.get("scale", 1.0) * h
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    for cx, cy, r, amp in spec.get("bumps", []):
        h = h + amp * np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (r * r)))
    h = blur(h.astype(np.float32), 2.0)
    return (h * (a > 0.002)).clip(0, 1)


# ---- the cut ------------------------------------------------------------------------------------


def cut(cfg: dict[str, Any], base: np.ndarray, seed_base: int = 0) -> list[dict[str, Any]]:
    """Per part: straight-alpha colour + alpha (full art size) and its relief map."""
    size = base.shape[0]
    parts = cfg["parts"]
    names = [p["name"] for p in parts]
    rgb, alpha = base[..., :3], base[..., 3]
    opaque = alpha > 0.002
    owner = np.full((size, size), -1, np.int32)
    for i, p in enumerate(parts):
        owner[(poly_mask(p["poly"], size) > 0.5) & opaque] = i
    strays = [i for i, p in enumerate(parts) if p.get("claimsStrays")]
    owner[opaque & (owner < 0)] = strays[0] if strays else 0

    feathers = [p.get("feather", 1.2) for p in parts]
    weights, unders, keeps = [], [], []
    mouth = cfg.get("mouth")
    for i, p in enumerate(parts):
        own = (owner == i).astype(np.float32)
        front = (owner > i).astype(np.float32)
        reach = int(np.ceil(3.2 * max(feathers[i + 1 :] + [1.0])))
        band = np.minimum(dilate(own, reach), front)
        extent = np.maximum(own, band)
        extent = np.maximum(extent, np.minimum(dilate(own, 8), (~opaque).astype(np.float32)))
        w = np.maximum(blur(extent, feathers[i]), band)
        under = np.zeros_like(own)
        for poly in p.get("underlay", []):
            under = np.maximum(under, poly_mask(poly, size))
        if mouth and p["name"] == mouth["head"]:
            under = np.maximum(under, (owner == names.index(mouth["jaw"])).astype(np.float32))
        weights.append(w.clip(0, 1))
        unders.append(blur(under, 2.5) * front)
        keeps.append(np.maximum(own, band))

    # front to back: each part takes what is still missing to reproduce the art's alpha, so the
    # rest pose is exact by construction; hidden continuations only where the front fully covers
    alphas: list[np.ndarray] = [np.zeros_like(alpha)] * len(parts)
    cover = np.zeros_like(alpha)
    for i in reversed(range(len(parts))):
        rest = np.where(cover < 0.999, (alpha - cover) / np.maximum(1 - cover, 1e-6), 0).clip(0, 1)
        a = np.maximum(weights[i] * rest, unders[i] * ((cover - 0.97) / 0.03).clip(0, 1))
        alphas[i] = a
        cover = cover + (1 - cover) * a

    out = []
    for i, p in enumerate(parts):
        a = alphas[i]
        own = (owner == i).astype(np.float32)
        hidden = ((a > 0.002) & (keeps[i] < 0.5)).astype(np.float32)
        col = inpaint_from(rgb, own, hidden, seed=seed_base + i) if hidden.any() else rgb.copy()
        if mouth and p["name"] == mouth["head"]:
            col = paint_mouth_cavity(col, (owner == names.index(mouth["jaw"])).astype(np.float32))
        out.append({"part": p, "col": col, "a": a, "depth": relief_map(p, a)})
    return out


def bbox(a: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(a > 0.004)
    size = a.shape[0]
    x0, y0 = max(0, int(xs.min()) - 2), max(0, int(ys.min()) - 2)
    x1, y1 = min(size, int(xs.max()) + 3), min(size, int(ys.max()) + 3)
    return x0, y0, x1 - x0, y1 - y0


def pack(items: list[tuple[str, int, int]]) -> tuple[dict[str, tuple[int, int]], int, int]:
    """Shelf packing, tallest first (ties by name): deterministic and good enough for ~15 tiles."""
    width = max(ATLAS_WIDTH, max(w for _, w, _ in items) + 2 * PAD)
    order = sorted(items, key=lambda t: (-t[2], t[0]))
    pos: dict[str, tuple[int, int]] = {}
    x = y = PAD
    row = 0
    for name, w, h in order:
        if x + w + PAD > width:
            x, y, row = PAD, y + row + PAD, 0
        pos[name] = (x, y)
        x += w + PAD
        row = max(row, h)
    height = y + row + PAD
    return pos, width, (height + 3) // 4 * 4


def to_u8(x: np.ndarray) -> np.ndarray:
    return (x * 255 + 0.5).clip(0, 255).astype(np.uint8)


def build(cfg: dict[str, Any], out_dir: Path, debug_dir: Path | None) -> float:
    variant = VARIANTS / cfg["id"]
    src = Image.open(variant / cfg["source"]).convert("RGBA")
    if src.width != src.height:
        raise SystemExit("the key art must be square")
    size = src.width
    base = np.asarray(src, np.float32) / 255.0
    # Key art is cropped at the bottom edge: feather the last rows so a bounce does not show a
    # ruler-straight cut under the paws.
    rows = np.arange(size, dtype=np.float32)[:, None]
    base[..., 3] *= np.clip((size - 1 - rows) / float(cfg.get("bottomFeather", 9)), 0, 1) ** 0.8

    layers = cut(cfg, base)
    tiles: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
    rects: dict[str, tuple[int, int, int, int]] = {}
    for layer in layers:
        name = layer["part"]["name"]
        x, y, w, h = bbox(layer["a"])
        rects[name] = (x, y, w, h)
        rgba = np.dstack([layer["col"], layer["a"]])[y : y + h, x : x + w]
        tiles[name] = (rgba, layer["depth"][y : y + h, x : x + w])
    eyes_out = []
    for eye in cfg["eyes"]:
        im = Image.open(variant / eye["file"]).convert("RGBA")
        tiles[eye["name"]] = (np.asarray(im, np.float32) / 255.0, None)
        tiles[eye["name"] + "#closed"] = (
            np.asarray(closed_eye(im, eye["ellipse"]), np.float32) / 255.0,
            None,
        )
        rects[eye["name"]] = (int(eye["at"][0]), int(eye["at"][1]), im.width, im.height)
        eyes_out.append(eye)

    pos, aw, ah = pack([(n, t[0].shape[1], t[0].shape[0]) for n, t in tiles.items()])
    atlas = np.zeros((ah, aw, 4), np.float32)
    depth = np.zeros((ah, aw), np.float32)
    for name, (rgba, dep) in tiles.items():
        x, y = pos[name]
        h, w = rgba.shape[:2]
        atlas[y : y + h, x : x + w] = rgba
        if dep is not None:
            depth[y : y + h, x : x + w] = dep
    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(to_u8(atlas), "RGBA").save(out_dir / "parts.png", optimize=True)
    Image.fromarray(to_u8(depth), "L").save(out_dir / "depth.png", optimize=True)

    def atlas_rect(name: str) -> list[int]:
        h, w = tiles[name][0].shape[:2]
        return [pos[name][0], pos[name][1], w, h]

    rig = {
        "version": 1,
        "id": cfg["id"],
        "generator": "scripts/relief/build_relief.py",
        "atlas": "parts.png",
        "depth": "depth.png",
        "artSize": size,
        "ground": cfg["ground"],
        "artHeight": cfg["artHeight"],
        "bones": cfg["bones"],
        "parts": [
            {
                "name": p["name"],
                "atlas": atlas_rect(p["name"]),
                "rect": list(rects[p["name"]]),
                "relief": p["relief"],
                "z": p["z"],
                "cell": p.get("cell", 4),
                "weights": p["weights"],
                **({"surface": p["surface"]} if "surface" in p else {}),
            }
            for p in cfg["parts"]
        ],
        "eyes": [
            {
                "name": e["name"],
                "atlas": atlas_rect(e["name"]),
                "closedAtlas": atlas_rect(e["name"] + "#closed"),
                "rect": list(rects[e["name"]]),
                "ellipse": e["ellipse"],
                "bone": e.get("bone", "head"),
                "surface": e.get("surface", "head"),
            }
            for e in eyes_out
        ],
        "order": cfg["order"],
        "headBones": cfg["headBones"],
        "neck": cfg["neck"],
        "headCenter": cfg["headCenter"],
        "bodyCenter": cfg["bodyCenter"],
        "lookAnchor": cfg["lookAnchor"],
        "light": cfg["light"],
        "focal": cfg.get("focal", 1500),
        "turn": cfg.get("turn", {"yaw": 0.42, "pitch": 0.28, "body": 0.32}),
        "roles": cfg["roles"],
        "poses": {k: v for k, v in cfg.get("poses", {}).items() if k != "comment"},
    }
    (out_dir / "relief.json").write_text(
        json.dumps(rig, indent=1) + "\n", encoding="utf-8", newline="\n"
    )

    psnr = report(base, layers)
    if debug_dir is not None:
        write_debug(cfg, src, layers, debug_dir)
    return psnr


def report(base: np.ndarray, layers: list[dict[str, Any]]) -> float:
    out = np.zeros_like(base)
    for layer in layers:  # "over": straight alpha in, premultiplied accumulation
        a = layer["a"][..., None]
        out[..., :3] = layer["col"] * a + out[..., :3] * (1 - a)
        out[..., 3] = layer["a"] + out[..., 3] * (1 - layer["a"])
    ref = base[..., :3] * base[..., 3:4]
    err = np.abs(out[..., :3] - ref)
    mse = float((err**2).mean())
    psnr = 10 * np.log10(1.0 / max(mse, 1e-12))
    print(
        f"rest-pose reconstruction: PSNR {psnr:.1f} dB, max colour err {err.max():.3f}, "
        f"max alpha err {np.abs(out[..., 3] - base[..., 3]).max():.3f}, "
        f"pixels >2% off: {int((err.max(-1) > 0.02).sum())}"
    )
    return psnr


def write_debug(
    cfg: dict[str, Any], src: Image.Image, layers: list[dict[str, Any]], d: Path
) -> None:
    """`overlay.png`: polygons, bones and eyes over the art at 2x - the picture to check a new
    creature against. `parts.png`: every layer on its own, side by side."""
    d.mkdir(parents=True, exist_ok=True)
    k = 2
    bg = Image.new("RGBA", src.size, (40, 40, 48, 255))
    bg.alpha_composite(src)
    im = bg.resize((src.width * k, src.height * k), Image.Resampling.LANCZOS)
    dr = ImageDraw.Draw(im, "RGBA")
    palette = [
        (255, 99, 71),
        (65, 105, 225),
        (50, 205, 50),
        (238, 130, 238),
        (255, 215, 0),
        (0, 206, 209),
        (255, 140, 0),
        (186, 85, 211),
        (127, 255, 212),
    ]
    for i, p in enumerate(cfg["parts"]):
        c = palette[i % len(palette)]
        dr.line([(x * k, y * k) for x, y in p["poly"] + p["poly"][:1]], fill=(*c, 255), width=2)
        for poly in p.get("underlay", []):
            dr.line([(x * k, y * k) for x, y in poly + poly[:1]], fill=(*c, 140), width=1)
        x0, y0 = p["poly"][0]
        dr.text((x0 * k + 3, y0 * k + 3), p["name"], fill=(*c, 255))
    for b in cfg["bones"]:
        (px, py), (tx, ty) = b["pivot"], b["tip"]
        col = (255, 255, 255, 230) if "jiggle" not in b else (255, 230, 120, 230)
        dr.line([(px * k, py * k), (tx * k, ty * k)], fill=col, width=2)
        dr.ellipse([px * k - 4, py * k - 4, px * k + 4, py * k + 4], outline=col, width=2)
    for e in cfg["eyes"]:
        x, y = e["at"]
        dr.rectangle([x * k, y * k, (x + 64) * k, (y + 64) * k], outline=(255, 255, 255, 120))
    im.save(d / "overlay.png")
    strip = Image.new("RGBA", (src.width * len(layers) // 2, src.height // 2), (40, 40, 48, 255))
    for i, layer in enumerate(layers):
        tile = Image.fromarray(to_u8(np.dstack([layer["col"], layer["a"]])), "RGBA").resize(
            (src.width // 2, src.height // 2)
        )
        strip.alpha_composite(tile, (i * src.width // 2, 0))
    strip.save(d / "layers.png")
    print(f"debug pictures: {d / 'overlay.png'}, {d / 'layers.png'}")


def enable_in_rig(variant_id: str, dir_name: str) -> None:
    """Switch the variant to the relief renderer (`rig.json` "relief" flag)."""
    path = VARIANTS / variant_id / "rig.json"
    rig = json.loads(path.read_text(encoding="utf-8"))
    rig["relief"] = {"dir": dir_name, "enabled": True}
    path.write_text(json.dumps(rig, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{path.name}: relief renderer enabled")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("creature", help="config name in scripts/relief/creatures, or a path to one")
    ap.add_argument("--out-dir", type=Path, help="default: public/variants/<id>/relief")
    ap.add_argument(
        "--debug-dir", type=Path, help="write overlay.png / layers.png here (not shipped)"
    )
    ap.add_argument(
        "--emit-config", type=Path, help="write the resolved config (after a template) here"
    )
    ap.add_argument(
        "--enable", action="store_true", help="switch the variant to the relief renderer"
    )
    args = ap.parse_args()
    cfg = load_config(args.creature)
    if args.emit_config:
        args.emit_config.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
    out_dir = args.out_dir or VARIANTS / cfg["id"] / "relief"
    psnr = build(cfg, out_dir, args.debug_dir)
    print(f"wrote {out_dir}")
    if psnr < MIN_PSNR:
        print(
            f"rest pose differs from the art (PSNR {psnr:.1f} < {MIN_PSNR}): check the polygons",
            file=sys.stderr,
        )
        return 1
    if args.enable:
        enable_in_rig(cfg["id"], out_dir.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
