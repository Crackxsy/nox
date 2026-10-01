# Pet art and rig scripts

Everything here is run by hand, from the project venv, and everything it produces is checked in.
None of it runs at build time.

```
.venv/Scripts/python.exe ui/pet/scripts/cut_out.py  SRC OUT     # background off a generated picture
.venv/Scripts/python.exe ui/pet/scripts/make_variant.py ID ...  # cut-out -> a whole rigged variant
.venv/Scripts/python.exe ui/pet/scripts/make_pose_frames.py ... # cut-out -> one key pose frame
.venv/Scripts/python.exe ui/pet/scripts/make_pet_layers.py      # the Meereswolf's own matte repair
.venv/Scripts/python.exe ui/pet/scripts/render_rig.py --variant ID   # review renders + cost
node ui/pet/scripts/weights_probe.mjs                           # bone weights at landmarks
.venv/Scripts/python.exe ui/pet/scripts/render_variants.py      # the older OP-1 concept gallery
```

## Adding a creature

Seven ship. A new one is the same four steps, and `src/__tests__/rigVariants.test.ts` reads the
variants off the directory, so it is tested the moment the folder exists.

1. **Cut it out.** `cut_out.py picture.png cut.png`. It works out for itself whether the backdrop
   is a transparency checkerboard, one flat colour, or a lit studio wall, and says which it chose.
   Two cases need a hand: a creature the colour of its backdrop (a grey cat on a grey wall, a white
   wolf on white) wants `--tolerance` well below the default - measure the border against the
   lightest fur rather than guessing - and a stubborn one can be told `--background` outright.
2. **Frame it.** `make_variant.py <id> --source cut.png` writes a grid preview and stops.
3. **Measure it.** Read the landmarks off that grid into `<id>.json`: the two eye centres, and a
   pivot for each bone. `inheritsFrom` lets a bone take another's clip tracks, which is how a
   dragon's wings move on the motion written for the Meereswolf's fins without being called fins.
4. **Build it.** `make_variant.py <id> --source cut.png --landmarks <id>.json`, then
   `render_rig.py --overlay-only --variant <id>` and look: every joint has to sit on the part it
   is named after. That picture is the check, not the test suite - a skeleton can be perfectly
   valid data and still be pinned to the wrong shoulder.

Key poses are separate art and arrive the same way: cut the picture out, then
`make_pose_frames.py cut.png <pose> <id>`, which scales it to the base drawing and drops it onto
the same ground line. That alignment is what makes a cross-dissolve read as the creature lying
down rather than as a cut to a different picture.

## Asking a generator for pose art

Two things decide whether the result is usable, and neither is the wording of the pose.

**Attach the creature.** Describing it gets a new animal that merely matches the description - the
first attempt at a sitting Chamster came back a different colour with a different face. Attaching
its base picture and asking for the same animal in a new pose keeps it the same animal.

**Ask for a green screen, not transparency.** Asked for transparency the generator bakes a
checkerboard into the pixels, and its light square is pure white, which is also what a white wolf
is made of. No threshold separates those. A flat chroma green keys cleanly off any creature that
is not itself green:

```
.venv/Scripts/python.exe ui/pet/scripts/cut_out.py raw.png cut.png     --background flat --tolerance 60 --max-size 512 --chroma
```

`--chroma` is what makes the fur edge right rather than merely present: it measures coverage from
the key colour left in each pixel, then takes that colour back out, so half a tuft of fur is
stored as half covered instead of as a green rim. The creatures with green of their own - the
Chamster's scales, the Mottenkatze's wings - survive it, a little desaturated at the edges, because
the flood still decides what is background and they are not connected to the screen.

| Script | What it owns |
| --- | --- |
| `pngio.py` | RGBA PNG read/write on `zlib` + `numpy`, so no script here needs Pillow. |
| `make_pet_layers.py` | Cuts the eye layers out of the source photo, fills the sockets behind them with fur, repairs the matte, and prints the measurements. Writes `public/variants/meereswolf/rig/`. |
| `render_rig.py` | Renders the rigged pet in headless Chromium: a still of every state on three desktops, three-second frame sequences of the motion, the bone overlay, and the measured frame cost. |
| `weights_probe.mjs` | Prints which bone owns which landmark. The influence radii in `rig.json` are the one thing that cannot be judged by looking at the picture. |
| `cut_out.py` | Separates a creature from its backdrop: checkerboard, flat colour, or a lit wall fitted as a surface. Keys on colour, texture and connectivity, because colour alone cannot tell grey fur from a grey wall. |
| `make_variant.py` | One cut-out plus a landmark file becomes a whole variant: base drawing, eye layers, filled sockets, `rig.json`, `sprites.json`. |
| `make_pose_frames.py` | Scales a cut-out pose to the base drawing and stands it on the same ground line. |
| `make_placeholder_sprites.py` | The generated placeholder sprite set, unrelated to the rig. |

## The matte, and what is wrong with the current source

`idle_1024.png` was keyed out of `source_gemini.jfif`, a JPEG with **no alpha channel at all** —
the "transparency checkerboard" in it is painted into the pixels. The cut also stored
*premultiplied* colour under a straight-alpha flag, which leaves every partially transparent pixel
too dark by exactly its own alpha. On a light desktop that reads as black stubble around the
creature.

`make_pet_layers.py` repairs what can be repaired: it reads the background colour off the fully
transparent pixels (measured, not guessed — it comes out black), erodes the matte by two pixels,
divides the colour back out, re-feathers by about a pixel, and lifts any edge pixel still far
darker than the fur beside it. Measured on the current source, at 1024 px:

| | mean border luminance | darkest border decile | worst border contrast on `#f5f5f7` | body contrast on `#f5f5f7` |
| --- | --- | --- | --- | --- |
| before | 0.102 | 0.000 | 16.6:1 | 2.75:1 |
| after | 0.378 | 0.076 | 15.5:1 | 2.74:1 |

The border now sits at the same luminance as the fur it belongs to (core: 0.352), which is the
number that decides whether the silhouette reads as fur or as stubble. The two contrast columns
barely move and that is honest: they are properties of the *photograph*, not of the matte. A white
wolf on a white desktop is 2.74:1 no matter how clean the edge is, and the worst border pixel is
the creature's own black nose seen through a soft edge. The renderer answers the first of those
with a contact shadow in light mode (`.pet-rig` in `styles.css`); the source answers it properly.

## What a better source image must provide

The rig reads whatever `rig.json` points at, so replacing the art is a matter of re-running
`make_pet_layers.py` and nudging the pivots. A good source is:

- **PNG or WebP with a real alpha channel**, straight (not premultiplied), cut from a shot taken
  against a solid keyable background — or rendered with alpha from the start. If the generator can
  only emit opaque images, a flat mid-grey or green backdrop is far better than a checkerboard,
  because one background colour can be keyed and decontaminated and two cannot.
- **2048 px square**, with the creature occupying about 85 % of the height and at least 5 % empty
  margin on every side, so the mesh has room to lean without clipping.
- **The same pose, camera and lighting** for every frame in a set. The rig blends between key poses
  by cross-dissolving them under one running mesh, and a dissolve between two different camera
  angles reads as a cut.
- **Eyes fully visible and open**, not in shadow, with the lower lid line distinguishable — that
  line is where the lid closes onto.
- **A mouth with a visible lip line**, ideally one frame closed and one slightly open. See below.
- **Ears and tail clear of the body outline**, so cutting or deforming them does not smear the
  torso behind them.

Key poses worth generating, in the order they would earn their keep: `sit` (the current pose, as a
clean source), `stand`, `lie`, `curl` (asleep), and `eat_0..n`. Name them in `rig.json` under
`poses` and the player will cross-dissolve to them; until the files exist they are reported as
missing and the pet keeps its base drawing.

## Why there is no mouth layer

The wolf's mouth is shut in the photograph and there is no lip line to cut along. A lower-jaw
layer would expose invented pixels the moment it moved — a dark wedge under the nose that the
script would have had to paint. The rig moves the muzzle through the mesh instead (`muzzle` bone,
a few per cent of vertical scale on `speak_idle`), which is a snout that works while the creature
talks rather than a mouth that opens. A real speaking mouth needs new source art with the mouth
visible; nothing in the code has to change to use it.
