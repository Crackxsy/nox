"""Cuts a generated picture of the creature out of its background.

The pictures arrive on a background: a flat colour, or the grey checkerboard some export tools bake
in when you ask for transparency and get a JPEG back. Either way the creature has to be separated
before it can become a rig pose, and doing that in an online converter is a step nobody can repeat
and that leaves no record of what it did.

Two ideas carry the result:

* **The background is what the border can reach.** Deleting every pixel that merely *looks* like
  background tears holes in the animal: it has pale fur and green scales that fall inside any
  tolerance wide enough to catch the checkerboard's greys. Flooding inward from the edge only
  removes what is connected to the outside.
* **Except when it is enclosed.** The field inside the curl of the tail is background too, and the
  border cannot reach it, so a closed region larger than a fur marking counts as well.

**The thresholds are tuned for a picture about 512 across**, because that is the size the art is
drawn at, and two of them - the texture window and the opening radius - are measured in pixels. A
1024 source keyed at full size reads as finer-grained than the same picture halved and lets more
through; a checkerboard halved reads as nothing but edges and lets nothing through. Hence
`--max-size 512` for a flat or lit backdrop, and full size for a checkerboard.

numpy only. `pngio` exists because Pillow is not a dependency of this project, and scipy arrives
only indirectly through another package - `labels_of` below is what `scipy.ndimage.label` does.

    python cut_out.py <picture.png> <cut-out.png>
    python cut_out.py <picture.png> <cut-out.png> --background checker
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from pngio import read_rgba, write_rgba

#: The two greys of a baked transparency checkerboard, as most tools draw it.
CHECKER_TONES = (211.0, 255.0)
#: Wide enough for the JPEG ringing along the checker edges, narrow enough to miss pale fur.
TONE_TOLERANCE = 26.0
#: The checkerboard is grey; fur never is.
CHROMA_LIMIT = 30.0
#: How far a flat background may drift from its corner colour and still be that background.
FLAT_TOLERANCE = 18.0
#: Below this, a closed region of background colour is a marking, not a hole.
ENCLOSED_MIN_AREA = 2500
#: ...and above this much detail it is fur, however large and however well its colour matches.
#: A grey creature on a grey backdrop defeats colour entirely - measured on the Mottenkatze, the
#: first percentile of the *animal* sits closer to the fitted backdrop than the backdrop's own
#: noise does. What the two never share is texture: the wall has none and fur is nothing but.
ENCLOSED_MAX_DETAIL = 8.0
#: The same test, applied to the flood from the border. Needed because a pale chest on a pale wall
#: is colour-matched *and* reachable from outside, so connectivity does not save it either.
FLOOD_MAX_DETAIL = 9.0
#: Texture is measured over a window, so the backdrop within one window of the creature's outline
#: reads as textured and would survive as a halo. These passes grow the background back into
#: anything that still matches the backdrop's colour, which is the halo and nothing else.
HALO_PASSES = 6
#: Fragments smaller than this share of the creature are not the creature. A cut-out of one animal
#: is one object; a detached speck is a surviving piece of shadow or an unkeyed corner of backdrop.
FRAGMENT_MIN_SHARE = 0.02
#: The mirror of FRAGMENT_MIN_SHARE: a hole this small, with creature all the way around it, is a
#: patch of flank that matched the wall - not a gap you can see through. Anything genuinely open,
#: like the triangle between an ear and the head, reaches the picture's border and is left alone.
HOLE_MAX_SHARE = 0.015
#: Radius of the opening applied to a lit backdrop's mask. A pale ruff on a pale wall is matched
#: in colour, smooth, and reachable through the gap between the front legs, so every test above
#: lets it through - but it only ever reaches in as a narrow tongue, and an opening of this radius
#: removes anything thinner than twice it while leaving the wall itself untouched.
OPENING_RADIUS = 9
#: Softens the silhouette by about a pixel, so the cut-out does not look stamped out.
FEATHER = 1.2
#: How far a pixel may sit from the fitted backdrop and still be that backdrop.
GRADIENT_TOLERANCE = 20.0
#: How much darker than the backdrop a cast shadow may be before it is an object instead.
#: The Chamster photograph puts its contact shadow at -120 and its darkest fur at -142, so depth
#: alone cannot separate them - see SHADOW_SMOOTHNESS for the test that does.
SHADOW_DEPTH = 130.0
#: A shadow dims every channel by much the same amount; a dark object tints as well as dims.
SHADOW_NEUTRALITY = 20.0
#: What finally tells a shadow from fur: a seamless backdrop has no detail, and darkening it adds
#: none. On the same photograph the backdrop measures 0.8, the cast shadow 6.7 and fur 12 to 22,
#: so requiring smoothness protects a shaded ear that brightness and colour alone would eat.
SHADOW_SMOOTHNESS = 10.0
#: Radius of the window local detail is measured over.
TEXTURE_RADIUS = 3
#: Width of the border band the backdrop surface is fitted from.
BORDER_BAND = 8
#: Above this spread along the border, one colour cannot describe the backdrop and a surface must.
GRADIENT_MIN_SPREAD = 24.0


def looks_like_checker(rgb: np.ndarray) -> bool:
    """Does the border look like a transparency checkerboard rather than one flat colour?"""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    grey = border.mean(axis=1)
    chroma = border.max(axis=1) - border.min(axis=1)
    if float(np.median(chroma)) > CHROMA_LIMIT:
        return False
    near = [np.abs(grey - tone) < TONE_TOLERANCE for tone in CHECKER_TONES]
    # Both tones along the border is what makes it a checkerboard rather than a grey wall.
    return all(float(part.mean()) > 0.15 for part in near)


def backdrop_surface(rgb: np.ndarray) -> np.ndarray:
    """The backdrop as a smooth quadratic surface, fitted to the picture's border band.

    A studio photograph is lit, not painted: the wall behind the creature drifts from dark in one
    corner to bright in another, and no single colour with a tolerance around it can describe that
    drift. Fitting the drift and comparing each pixel against the *local* prediction can. Only the
    border band is used, because that is the part of the picture the creature is known to be out of.
    """
    height, width = rgb.shape[:2]
    rows, columns = np.mgrid[0:height, 0:width]
    y = rows / max(1, height - 1)
    x = columns / max(1, width - 1)
    terms = np.stack([np.ones_like(x), x, y, x * x, y * y, x * y], axis=-1)

    edge = np.zeros((height, width), dtype=bool)
    edge[:BORDER_BAND] = edge[-BORDER_BAND:] = True
    edge[:, :BORDER_BAND] = edge[:, -BORDER_BAND:] = True

    surface = np.empty_like(rgb)
    for channel in range(3):
        coefficients, *_ = np.linalg.lstsq(terms[edge], rgb[..., channel][edge], rcond=None)
        surface[..., channel] = terms @ coefficients
    return surface


def looks_like_gradient(rgb: np.ndarray) -> bool:
    """Does the border drift too much for one colour to stand for the whole backdrop?"""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    grey = border.mean(axis=1)
    return float(np.percentile(grey, 95) - np.percentile(grey, 5)) > GRADIENT_MIN_SPREAD


def _box_mean(values: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a square window, by summed-area table."""
    padded = np.pad(values, radius, mode="edge")
    sums = np.pad(padded.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    height, width = values.shape
    span = 2 * radius + 1
    return (
        sums[span : height + span, span : width + span]
        - sums[0:height, span : width + span]
        - sums[span : height + span, 0:width]
        + sums[0:height, 0:width]
    ) / (span * span)


def local_detail(rgb: np.ndarray) -> np.ndarray:
    """Standard deviation of brightness in a small window: how textured each place is."""
    grey = rgb.mean(axis=2)
    mean = _box_mean(grey, TEXTURE_RADIUS)
    return np.sqrt(np.maximum(_box_mean(grey * grey, TEXTURE_RADIUS) - mean * mean, 0.0))


def candidate_mask(rgb: np.ndarray, *, kind: str) -> np.ndarray:
    """Pixels that *could* be background, before asking whether they reach the outside."""
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    if kind == "checker":
        grey = rgb.mean(axis=2)
        near = np.minimum.reduce([np.abs(grey - tone) for tone in CHECKER_TONES])
        # No texture test here, unlike the lit backdrop below. It was tried, to separate a white
        # creature from the white square of a checkerboard, and it cannot work on a pattern: the
        # squares are only tens of pixels across, so most of the board lies within a texture
        # window of an edge and reads as textured. On a picture with fine squares it removed
        # nothing at all. A white creature needs a green screen, not a cleverer threshold - see
        # the chroma key below.
        return (near < TONE_TOLERANCE) & (chroma < CHROMA_LIMIT)
    if kind == "gradient":
        difference = rgb - backdrop_surface(rgb)
        close = np.abs(difference).max(axis=2) < GRADIENT_TOLERANCE
        # A cast shadow is the backdrop with the light taken away: dimmer in every channel by
        # about the same amount. A dark object tints as it dims, which is what the spread catches.
        spread = difference.max(axis=2) - difference.min(axis=2)
        shadow = (
            (difference.max(axis=2) < GRADIENT_TOLERANCE)
            & (spread < SHADOW_NEUTRALITY)
            & (difference.mean(axis=2) > -SHADOW_DEPTH)
            & (local_detail(rgb) < SHADOW_SMOOTHNESS)
        )
        # Colour alone cannot key a grey creature off a grey wall; see ENCLOSED_MAX_DETAIL.
        return (close | shadow) & (local_detail(rgb) < FLOOD_MAX_DETAIL)
    corners = np.stack([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
    colour = np.median(corners, axis=0)
    return np.abs(rgb - colour).max(axis=2) < FLAT_TOLERANCE


def labels_of(mask: np.ndarray) -> np.ndarray:
    """Connected components, by propagating the lowest index through each region.

    Every pixel starts as its own label and repeatedly takes the smallest label among itself and
    its four neighbours inside the mask, until nothing changes. A region then carries the index of
    its lowest pixel, which is all the caller needs.
    """
    height, width = mask.shape
    labels = np.where(mask, np.arange(height * width).reshape(height, width), -1)
    while True:
        nxt = labels.copy()
        for axis, step in ((0, 1), (0, -1), (1, 1), (1, -1)):
            rolled = np.roll(labels, step, axis=axis)
            # A roll wraps around; the wrapped edge must not join opposite sides of the picture.
            if axis == 0:
                rolled[0 if step == 1 else -1, :] = -1
            else:
                rolled[:, 0 if step == 1 else -1] = -1
            joined = (rolled >= 0) & mask
            nxt = np.where(joined, np.minimum(nxt, np.where(joined, rolled, 0)), nxt)
        nxt = np.where(mask, nxt, -1)
        if np.array_equal(nxt, labels):
            return labels
        labels = nxt


def background_of(rgb: np.ndarray, *, kind: str) -> np.ndarray:
    """What is really background: connected to the border, or a large enclosed field."""
    candidate = candidate_mask(rgb, kind=kind)
    labels = labels_of(candidate)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    keep = set(np.unique(border[border >= 0]).tolist())

    detail = local_detail(rgb)
    present, areas = np.unique(labels[labels >= 0], return_counts=True)
    for label, area in zip(present, areas, strict=True):
        if area <= ENCLOSED_MIN_AREA:
            continue
        # Enclosed and large is not enough. The field inside a curled tail is flat; a patch of
        # grey flank that happens to match the wall is not, and used to be punched out as a hole.
        if float(np.median(detail[labels == label])) < ENCLOSED_MAX_DETAIL:
            keep.add(int(label))

    if not keep:
        return np.zeros_like(candidate)
    found = np.isin(labels, list(keep))
    if kind == "gradient":
        found = opened(found, OPENING_RADIUS)
        found = regrow_halo(rgb, found, kind=kind)
    return found


def _grow(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def opened(mask: np.ndarray, radius: int) -> np.ndarray:
    """Erode then dilate: thin tongues disappear, broad regions keep their shape."""
    shrunk = mask
    for _ in range(radius):
        shrunk = ~_grow(~shrunk)
    for _ in range(radius):
        shrunk = _grow(shrunk)
    return shrunk & mask


def regrow_halo(rgb: np.ndarray, found: np.ndarray, *, kind: str = "gradient") -> np.ndarray:
    """Grow the background into neighbouring pixels that still match the fitted backdrop.

    Only the halo the texture window left behind can satisfy both conditions - touching known
    background and sitting on the backdrop's own colour - so this recovers the outline without
    reaching the fur the texture test was there to protect.
    """
    if kind == "checker":
        grey = rgb.mean(axis=2)
        near = np.minimum.reduce([np.abs(grey - tone) for tone in CHECKER_TONES])
        matches = (near < TONE_TOLERANCE) & (rgb.max(axis=2) - rgb.min(axis=2) < CHROMA_LIMIT)
    else:
        difference = rgb - backdrop_surface(rgb)
        matches = np.abs(difference).max(axis=2) < GRADIENT_TOLERANCE
    grown = found.copy()
    for _ in range(HALO_PASSES):
        neighbours = np.zeros_like(grown)
        neighbours[1:, :] |= grown[:-1, :]
        neighbours[:-1, :] |= grown[1:, :]
        neighbours[:, 1:] |= grown[:, :-1]
        neighbours[:, :-1] |= grown[:, 1:]
        added = neighbours & matches & ~grown
        if not added.any():
            break
        grown |= added
    return grown


def feather(mask: np.ndarray, radius: float) -> np.ndarray:
    """A soft edge: a small box blur of the silhouette, remapped back towards 0 and 1."""
    size = max(1, int(round(radius * 2)) | 1)
    padded = np.pad(mask.astype(np.float64), size, mode="edge")
    sums = np.pad(padded.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    height, width = mask.shape
    span = 2 * size + 1
    blurred = np.empty((height, width), dtype=np.float64)
    for y in range(height):
        top, bottom = y, y + span
        blurred[y] = (
            sums[bottom, span : width + span]
            - sums[top, span : width + span]
            - sums[bottom, 0:width]
            + sums[top, 0:width]
        ) / (span * span)
    return np.clip((blurred - 0.35) / 0.4, 0.0, 1.0)


def fill_holes(alpha: np.ndarray) -> np.ndarray:
    """Close small pockets of background that the creature completely encloses."""
    empty = alpha <= 0.5
    if not empty.any():
        return alpha
    labels = labels_of(empty)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    outside = set(np.unique(border[border >= 0]).tolist())
    limit = max(1.0, float((alpha > 0.5).sum()) * HOLE_MAX_SHARE)
    present, areas = np.unique(labels[labels >= 0], return_counts=True)
    holes = [
        int(label)
        for label, area in zip(present, areas, strict=True)
        if int(label) not in outside and area < limit
    ]
    if not holes:
        return alpha
    return np.where(np.isin(labels, holes), 1.0, alpha)


def drop_fragments(alpha: np.ndarray) -> np.ndarray:
    """Keep the creature and throw away anything detached from it.

    The keying leaves specks behind: a piece of contact shadow too dark to match the backdrop, a
    corner of checkerboard the tolerance missed. They are always small and never touching the
    animal, which is exactly what makes them separable here rather than by another threshold.
    """
    solid = alpha > 0.5
    if not solid.any():
        return alpha
    labels = labels_of(solid)
    present, areas = np.unique(labels[labels >= 0], return_counts=True)
    if len(areas) == 0:
        return alpha
    keep = present[areas >= areas.max() * FRAGMENT_MIN_SHARE]
    return np.where(np.isin(labels, keep), alpha, 0.0)


def halve(image: np.ndarray) -> np.ndarray:
    """Average 2x2 blocks. The source has no alpha yet, so no premultiplication is needed."""
    height, width = (side - side % 2 for side in image.shape[:2])
    block = image[:height, :width].astype(np.float64).reshape(height // 2, 2, width // 2, 2, -1)
    return block.mean(axis=(1, 3))


def backdrop_colour(rgb: np.ndarray, kind: str) -> np.ndarray:
    """What was behind the creature, per pixel, as far as it can be known."""
    if kind == "gradient":
        return backdrop_surface(rgb)
    if kind == "checker":
        grey = rgb.mean(axis=2)
        nearest = min(CHECKER_TONES, key=lambda tone: abs(float(np.median(grey)) - tone))
        pick = np.where(
            np.abs(grey - CHECKER_TONES[0]) < np.abs(grey - CHECKER_TONES[1]),
            CHECKER_TONES[0],
            CHECKER_TONES[1],
        )
        return np.repeat(np.where(np.isfinite(pick), pick, nearest)[..., None], 3, axis=2)
    corners = np.stack([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
    return np.broadcast_to(np.median(corners, axis=0), rgb.shape).copy()


def decontaminate(rgb: np.ndarray, alpha: np.ndarray, backdrop: np.ndarray) -> np.ndarray:
    """Take the backdrop back out of the half-transparent edge.

    A pixel on the silhouette is a mixture: `C = a*F + (1-a)*B`, the fur F showing through over
    whatever was behind it. Storing C and calling it the fur leaves the backdrop smeared round the
    outline - a green rim off a chroma key, grey stubble off a studio wall - which the compositor
    then draws over the desktop. B is known and a has just been measured, so F can simply be
    solved for. Below a sliver of coverage the division is noise, so those pixels are left alone;
    they are almost invisible anyway.
    """
    coverage = np.maximum(alpha, 1e-3)[..., None]
    recovered = (rgb - (1.0 - coverage) * backdrop) / coverage
    usable = (alpha > 0.04)[..., None]
    return np.clip(np.where(usable, recovered, rgb), 0.0, 255.0)


def chroma_matte(rgb: np.ndarray, key: np.ndarray, outside: np.ndarray) -> np.ndarray:
    """Coverage from how much of the key colour is left in a pixel, not from how dark it is.

    The obvious measure - distance from the key colour - fails on exactly the creatures a chroma
    key is for. Shadowed white fur is numerically closer to green than lit fur is, so a distance
    ramp reads the wolf's own shaded flank as half transparent and then unmixes green out of it,
    which turns it magenta. Measured on the attempt that did: fur in shadow came out at a
    coverage of 0.5 and the whole animal went purple.

    What separates a green screen from anything else is not its brightness but that its green runs
    far ahead of its red and blue. Fur does not, at any brightness. So the excess carries the
    answer: full excess means the pixel is all screen, none means it is all creature, and half of
    it means half covered - which is what the edge of a tuft of fur actually is.
    """
    dominant = int(np.argmax(key.reshape(-1, 3).mean(axis=0)))
    others = [channel for channel in range(3) if channel != dominant]
    excess = rgb[..., dominant] - np.maximum(rgb[..., others[0]], rgb[..., others[1]])
    reference = float(
        np.median(key[..., dominant] - np.maximum(key[..., others[0]], key[..., others[1]]))
    )
    if reference <= 1.0:  # not a chroma screen at all; fall back to a hard matte
        return np.where(outside, 0.0, 1.0)
    covered = np.clip(excess / reference, 0.0, 1.0)
    # The flood still has the last word: fur that happens to be greenish but is not connected to
    # the screen stays opaque.
    return np.where(outside, 0.0, 1.0 - covered)


def main() -> int:
    global FLAT_TOLERANCE, GRADIENT_TOLERANCE  # noqa: PLW0603
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--chroma",
        action="store_true",
        help=(
            "the backdrop is a chroma key. Builds a soft matte from the colour distance instead "
            "of a hard one, so a half-covered pixel is stored as half covered and the key colour "
            "can be taken back out of it - without this a green rim survives along the fur"
        ),
    )
    parser.add_argument(
        "--max-size",
        type=int,
        help=(
            "key at this resolution instead of the source's. The flood fill costs more than "
            "linearly in pixels, and a pose frame ends up at 512 anyway, so halving first is "
            "most of the time back for an edge nobody will see at the size it is drawn. "
            "Not for a checkerboard: its squares halve with the picture until every pixel is "
            "near an edge, every pixel therefore counts as textured, and nothing is removed"
        ),
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        help=(
            "how far a pixel may sit from the backdrop and still be background. The default suits "
            "a creature that contrasts with its backdrop; a white wolf on a white wall needs a "
            "much tighter one, and the picture itself will tell you which - measure the border "
            "against the lightest fur before guessing"
        ),
    )
    parser.add_argument(
        "--background",
        choices=("auto", "checker", "flat", "gradient"),
        default="auto",
        help="what the picture sits on; auto looks at the border",
    )
    args = parser.parse_args()

    if args.tolerance is not None:
        FLAT_TOLERANCE = GRADIENT_TOLERANCE = args.tolerance

    image = read_rgba(args.source)
    if args.max_size and max(image.shape[:2]) > args.max_size:
        while max(image.shape[:2]) > args.max_size * 2:
            image = halve(image)
        image = halve(image)
    rgb = image[..., :3].astype(np.float64)
    if args.background == "auto":
        if looks_like_checker(rgb):
            kind = "checker"
        else:
            kind = "gradient" if looks_like_gradient(rgb) else "flat"
    else:
        kind = args.background

    outside = background_of(rgb, kind=kind)
    if args.chroma:
        alpha = chroma_matte(rgb, backdrop_colour(rgb, kind), outside)
    else:
        alpha = feather(~outside, FEATHER)
    alpha = fill_holes(drop_fragments(alpha))
    clean = decontaminate(rgb, alpha, backdrop_colour(rgb, kind))
    write_rgba(args.destination, np.dstack([clean, alpha * 255.0]).astype(np.uint8))

    solid = alpha > 0.5
    rows, columns = np.nonzero(solid)
    described = {"checker": "checkerboard", "flat": "flat colour", "gradient": "lit backdrop"}[kind]
    if len(rows) == 0:
        print(f"{args.destination.name}: nothing left - the {described} guess was probably wrong")
        return 1
    print(
        f"{args.destination.name}: {described} removed, "
        f"creature is {solid.mean():.1%} of the frame, "
        f"x {columns.min()}-{columns.max()}, y {rows.min()}-{rows.max()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
