"""Turn one cut-out photograph into a complete rigged variant.

`make_pet_layers.py` did this for the Meereswolf and only for the Meereswolf: its landmarks are
module constants and its output path is hard-coded. That was honest while there was one creature.
There are now several, and doing the same twelve steps by hand for each is how a second variant
ends up subtly different from the first - a different ground line, a different eye size, a skeleton
that drifted. This script is those steps, written down once.

It runs in two passes, because the middle step needs eyes:

    python make_variant.py chamster --source cut/chamster.png
        Frames the creature and writes a grid preview next to it. Read the landmarks off that.

    python make_variant.py chamster --source cut/chamster.png --landmarks chamster.json
        Writes base.png, the eye layers, rig.json, sprites.json and idle.png.

**Why the framing is not a matter of taste.** The rig cross-dissolves between the base drawing and
a key pose under one running mesh. If the two are different sizes, or stand on different ground
lines, the dissolve reads as a cut instead of a move. `--ground` and `--extent` therefore default
to the values the shipped pose frames were made at, and the framing is printed so a mismatch is
visible before anything is written.

**Why the sockets are filled.** The eyes are separate layers so a blink can be a lid closing over
an eyeball. The base drawing underneath still has eyes in it, so without filling them the creature
blinks over its own open eye. The fill is an inverse-distance blend of the ring around the socket:
smooth, and seen only for the 190 ms a blink lasts.

numpy only - `pngio` exists because Pillow is not a dependency of this project.
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path

import numpy as np
from pngio import read_rgba, write_rgba

PET_DIR = Path(__file__).resolve().parents[1]
VARIANTS = PET_DIR / "public" / "variants"

#: The frame every variant is drawn into. 512 is what the shipped art uses.
SIZE = 512
#: Where the creature's feet sit. Taken from the shipped pose frames, which ground at 0.992-0.996.
GROUND = 509
#: The creature's longest side, in pixels. Chosen so a sitting creature matches the sitting pose
#: frames (73 % of the frame) and still has room to lean without clipping the edge.
EXTENT = 378
#: Half-width of an eye layer, as a fraction of the frame.
EYE_HALF = 0.062
#: How far past the eye layer the socket fill reaches, so a dark eye rim does not survive it.
SOCKET_OVERSHOOT = 1.06
#: Which variant's clips every other variant inherits. They are generic body motion, not anatomy.
CLIP_SOURCE = "meereswolf"


def resize(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Bilinear resample. Straight numpy, so the scripts stay Pillow-free."""
    source_height, source_width = image.shape[:2]
    ys = np.linspace(0, source_height - 1, height)
    xs = np.linspace(0, source_width - 1, width)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, source_height - 1)
    x1 = np.minimum(x0 + 1, source_width - 1)
    wy = (ys - y0)[:, None, None]
    wx = (xs - x0)[None, :, None]
    return (
        image[y0][:, x0] * (1 - wy) * (1 - wx)
        + image[y0][:, x1] * (1 - wy) * wx
        + image[y1][:, x0] * wy * (1 - wx)
        + image[y1][:, x1] * wy * wx
    )


def frame_creature(cut: np.ndarray, *, extent: int, ground: int) -> np.ndarray:
    """Trim to the creature, scale it to `extent`, and stand it on `ground`.

    The scaling runs on premultiplied colour. Resampling straight RGBA pulls whatever is stored in
    the fully transparent pixels into the edge, which on a cut-out is the background it was cut
    from - the fringe of dark stubble `make_pet_layers.py` had to repair afterwards.
    """
    rows, columns = np.nonzero(cut[..., 3] > 127)
    if len(rows) == 0:
        raise SystemExit("the source has no opaque pixels; was it cut out?")
    trimmed = cut[rows.min() : rows.max() + 1, columns.min() : columns.max() + 1]

    alpha = trimmed[..., 3:4] / 255.0
    premultiplied = np.dstack([trimmed[..., :3] * alpha, trimmed[..., 3:4]])

    height, width = trimmed.shape[:2]
    scale = extent / max(height, width)
    new_height, new_width = int(round(height * scale)), int(round(width * scale))

    framed = np.zeros((SIZE, SIZE, 4))
    top = ground - new_height + 1
    left = (SIZE - new_width) // 2
    if top < 0:
        raise SystemExit(f"{new_height} px tall will not stand on y={ground}; lower --extent")
    framed[top : top + new_height, left : left + new_width] = resize(
        premultiplied, new_width, new_height
    )
    out_alpha = np.maximum(framed[..., 3:4] / 255.0, 1e-6)
    return np.dstack([np.clip(framed[..., :3] / out_alpha, 0, 255), framed[..., 3:4]])


def write_grid(image: np.ndarray, destination: Path) -> None:
    """The base drawing under a 5 % grid, which is how the landmarks are read off it."""
    alpha = image[..., 3:4] / 255.0
    over = image[..., :3] * alpha + 255.0 * (1 - alpha)
    for step in range(1, 20):
        at = int(SIZE * step / 20)
        over[at : at + 1, :] = [220, 0, 0] if step % 2 == 0 else [255, 185, 185]
        over[:, at : at + 1] = [0, 0, 220] if step % 2 == 0 else [185, 185, 255]
    write_rgba(destination, np.dstack([over, np.full((SIZE, SIZE), 255.0)]).astype(np.uint8))


def report(image: np.ndarray) -> None:
    rows, columns = np.nonzero(image[..., 3] > 127)
    print(
        f"  framed: x {columns.min()}-{columns.max()} "
        f"({columns.min() / SIZE:.3f}-{columns.max() / SIZE:.3f})  "
        f"y {rows.min()}-{rows.max()}  ground {rows.max() / SIZE:.3f}  "
        f"height {(rows.max() - rows.min()) / SIZE:.1%}"
    )


def fill_socket(rgb: np.ndarray, centre: tuple[float, float], radius: float) -> None:
    """Replace a disc with an inverse-distance blend of the ring around it, in place.

    Diffusing from the four neighbours - the obvious way - leaves a cross-shaped smear, because the
    axes reach the boundary sooner than the diagonals do. Weighting every boundary pixel by
    distance has no preferred direction and costs one matrix multiply per block.
    """
    height, width = rgb.shape[:2]
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float64)
    distance = np.sqrt((xs - centre[0]) ** 2 + (ys - centre[1]) ** 2)
    hole = distance < radius
    ring = (distance >= radius) & (distance < radius + 7)
    ring_rows, ring_columns = np.nonzero(ring)
    ring_colour = rgb[ring]
    hole_rows, hole_columns = np.nonzero(hole)
    for start in range(0, len(hole_rows), 2000):
        block_rows = hole_rows[start : start + 2000]
        block_columns = hole_columns[start : start + 2000]
        squared = (block_rows[:, None] - ring_rows[None, :]) ** 2 + (
            block_columns[:, None] - ring_columns[None, :]
        ) ** 2
        weights = 1.0 / np.maximum(squared, 1.0) ** 1.5
        weights /= weights.sum(axis=1, keepdims=True)
        rgb[block_rows, block_columns] = weights @ ring_colour


def clip_library(bone_names: set[str]) -> OrderedDict:
    """The shared clips, with any track for a bone this creature lacks dropped.

    A track naming a bone that does not exist is not inert: the loader rejects the whole rig, and
    the pet silently falls back to a static frame. Dropping them here is what lets a creature with
    no fins inherit the clips written for one with fins.
    """
    source = json.loads((VARIANTS / CLIP_SOURCE / "rig.json").read_text(encoding="utf-8"))
    clips: OrderedDict = OrderedDict()
    for name, clip in source["clips"].items():
        kept = [track for track in clip["tracks"] if track["bone"] in bone_names]
        if not kept:
            continue
        copy = OrderedDict(clip)
        copy["tracks"] = kept
        clips[name] = copy
    return clips


def inherit_tracks(clips: OrderedDict, landmarks: dict) -> None:
    """Give a bone the motion written for another one, in place.

    The clips are shared, and they name the Meereswolf's anatomy. A dragon's wings and a wolf's
    gill fins sit in the same place and want the same small flutter, but calling a wing `fin.l` so
    it inherits that track would be a lie in the data. `inheritsFrom` copies the motion instead,
    so the bone can be named after the thing it actually moves.
    """
    for name, spec in landmarks["bones"].items():
        source = spec.get("inheritsFrom")
        if not source:
            continue
        for clip in clips.values():
            copies = [dict(track, bone=name) for track in clip["tracks"] if track["bone"] == source]
            clip["tracks"].extend(copies)


def drop_unknown_tracks(clips: OrderedDict, bone_names: set[str]) -> None:
    """Remove what is left of the donor bones, in place.

    The donors had to survive `clip_library` long enough to be copied. Left behind they would name
    a bone this creature does not have, which makes the loader reject the whole rig.
    """
    for name in list(clips):
        clips[name]["tracks"] = [t for t in clips[name]["tracks"] if t["bone"] in bone_names]
        if not clips[name]["tracks"]:
            del clips[name]


#: Grid cells per side. 30 rather than 22 so the jaw's small reach moves the chin, not the cheeks.
MESH_CELLS = 30

#: Where the jaw sits relative to the muzzle, and how far it reaches. The photographs have closed
#: mouths and no lip line to cut along, so speech is a chin that drops - a hand puppet's jaw - and
#: the speaking clip (`speak_idle`) drives it in an uneven syllable rhythm. Measured on three
#: creatures at the 260 px window: 5-10 px at the strongest syllable; below that it did not read
#: as speech at all.
JAW_BELOW_MUZZLE = 0.065
JAW_RADIUS = 0.07


def add_jaw(bones: list) -> None:
    """A `jaw` under the muzzle, unless the landmarks already name one."""
    names = [bone["name"] for bone in bones]
    if "jaw" in names or "muzzle" not in names:
        return
    muzzle = bones[names.index("muzzle")]
    jaw = OrderedDict(
        name="jaw",
        parent="muzzle",
        pivot=[muzzle["pivot"][0], round(muzzle["pivot"][1] + JAW_BELOW_MUZZLE, 5)],
        restAngle=muzzle["restAngle"],
    )
    jaw["influence"] = OrderedDict(radius=JAW_RADIUS, falloff=0.6)
    bones.insert(names.index("muzzle") + 1, jaw)


def build_rig(variant: str, landmarks: dict, eye_rects: dict, poses: list[str]) -> OrderedDict:
    bones = []
    for name, spec in landmarks["bones"].items():
        bone = OrderedDict(
            name=name,
            parent=spec.get("parent"),
            pivot=[round(float(value), 5) for value in spec["pivot"]],
            restAngle=spec.get("restAngle", -90),
        )
        if spec.get("channelOnly"):
            bone["channelOnly"] = True
        bone["influence"] = OrderedDict(
            radius=spec.get("radius", 0.2), falloff=spec.get("falloff", 1.0)
        )
        bones.append(bone)
    add_jaw(bones)

    layers = [
        OrderedDict(
            name=name.replace("_", "."),
            file=f"{name}.png",
            rect=eye_rects[name]["rect"],
            grid=OrderedDict(columns=3, rows=3),
            lid=OrderedDict(bone=name.replace("eye_", "lid.")),
        )
        for name in ("eye_l", "eye_r")
    ]

    names = {bone["name"] for bone in bones}
    donors = {
        spec["inheritsFrom"]
        for spec in landmarks["bones"].values()
        if spec.get("inheritsFrom")
    }
    clips = clip_library(names | donors)
    inherit_tracks(clips, landmarks)
    drop_unknown_tracks(clips, names)

    return OrderedDict(
        id=variant,
        base="base.png",
        assetDir="rig",
        mesh=OrderedDict(columns=MESH_CELLS, rows=MESH_CELLS),
        bones=bones,
        layers=layers,
        poses=OrderedDict((pose, OrderedDict(file=f"pose_{pose}.png")) for pose in sorted(poses)),
        clips=clips,
    )


def write_json(destination: Path, payload: OrderedDict) -> None:
    """Write pretty JSON with Unix line endings, like the rest of the repository."""
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", help="the variant id, e.g. chamster")
    parser.add_argument("--source", required=True, type=Path, help="the cut-out PNG")
    parser.add_argument("--landmarks", type=Path, help="landmark JSON; omit for the first pass")
    parser.add_argument("--grid", action="store_true", help="also write a grid preview")
    parser.add_argument("--extent", type=int, default=EXTENT)
    parser.add_argument("--ground", type=int, default=GROUND)
    parser.add_argument("--name", help="display name for sprites.json")
    args = parser.parse_args()

    out = VARIANTS / args.variant
    (out / "rig").mkdir(parents=True, exist_ok=True)

    cut = read_rgba(args.source).astype(np.float64)
    base = frame_creature(cut, extent=args.extent, ground=args.ground)
    print(f"{args.variant}:")
    report(base)

    if args.grid or not args.landmarks:
        preview = args.source.with_name(f"{args.variant}-grid.png")
        write_grid(base, preview)
        print(f"  grid preview -> {preview}")
    if not args.landmarks:
        print("  read the landmarks off that grid, then re-run with --landmarks")
        return 0

    landmarks = json.loads(args.landmarks.read_text(encoding="utf-8"))
    half = landmarks.get("eyeHalf", EYE_HALF) * SIZE

    # The eye layers come out of the *unfilled* base, so they carry the real eye.
    eye_rects = {}
    for name in ("eye_l", "eye_r"):
        centre_x, centre_y = (value * SIZE for value in landmarks["eyes"][name])
        x0, x1 = int(round(centre_x - half)), int(round(centre_x + half))
        y0, y1 = int(round(centre_y - half)), int(round(centre_y + half))
        write_rgba(out / "rig" / f"{name}.png", base[y0:y1, x0:x1].astype(np.uint8))
        eye_rects[name] = {
            "rect": [
                round(x0 / SIZE, 5),
                round(y0 / SIZE, 5),
                round(x1 / SIZE, 5),
                round(y1 / SIZE, 5),
            ],
            "centre": (centre_x, centre_y),
        }
        print(f"  {name}.png {x1 - x0}x{y1 - y0}  rect {eye_rects[name]['rect']}")

    rgb = base[..., :3].copy()
    for name in ("eye_l", "eye_r"):
        fill_socket(rgb, eye_rects[name]["centre"], half * SOCKET_OVERSHOOT)
    filled = np.dstack([rgb, base[..., 3:4]]).astype(np.uint8)
    write_rgba(out / "rig" / "base.png", filled)
    write_rgba(out / "idle.png", filled)

    poses = [path.stem.removeprefix("pose_") for path in sorted((out / "rig").glob("pose_*.png"))]
    rig = build_rig(args.variant, landmarks, eye_rects, poses)
    write_json(out / "rig.json", rig)

    sprites = OrderedDict(
        id=args.variant,
        name=args.name or landmarks.get("name", args.variant),
        frames=OrderedDict(idle=["idle.png"]),
        fps=6,
        anchor=[0.5, 0.5],
        scale=1.0,
    )
    write_json(out / "sprites.json", sprites)
    print(
        f"  rig.json: {len(rig['bones'])} bones, {len(rig['clips'])} clips, "
        f"poses {poses or 'none yet'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
