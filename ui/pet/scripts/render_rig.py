"""Render the rigged pet headlessly, so its motion can be judged instead of imagined.

Three things come out of this, all into `--out`:

* `state-<name>-<bg>.png` - one still of every pet state on a dark, a light and a busy wallpaper.
* `clip-<name>/frame-NNN.png` - three seconds of a named clip as numbered frames, driven by
  Playwright's virtual clock so the sequence is reproducible rather than a lucky capture.
* `overlay-bones.png` - the skeleton and the mesh drawn over the base texture, which is how the
  pivots in `rig.json` were placed and how they are re-checked after an edit.

It also reports the cost: frames rendered, mean and 95th-percentile frame time, and the share of
one core that works out to. Run with the project venv:

    .venv/Scripts/python.exe ui/pet/scripts/render_rig.py [--skip-build] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
from playwright.sync_api import Browser, Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pngio import read_rgba, write_rgba  # noqa: E402  (needs the path above)

LOG = logging.getLogger("nox.pet." + Path(__file__).stem)

PET_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = PET_DIR / "dist"
VARIANT = "sprite:meereswolf"
RIG_JSON = PET_DIR / "public" / "variants" / "meereswolf" / "rig.json"
BASE_TEXTURE = PET_DIR / "public" / "variants" / "meereswolf" / "rig" / "base.png"

SIZE = 260
WINDOW = (260, 300)
STATES = ["normal", "happy", "thinking", "speaking", "privacy", "sleeping"]
#: Clips rendered as sequences: the state that puts the rig into them, and how they are filmed.
#:
#: The slow loops are filmed under Playwright's virtual clock, stepped 100 ms at a time, so their
#: phase is exact and two runs are comparable. The short unprompted motions cannot be: however
#: small a step the clock is asked for, it advances the page by a whole rendered frame and a
#: 190 ms blink comes out as one picture. Those are filmed in real time instead - slower and not
#: reproducible to the millisecond, but it is the only way to see the motion.
CLIP_SEQUENCES = {
    "breathe": ("normal", "clock"),
    "blink": ("normal", "realtime"),
    "ear_flick": ("normal", "realtime"),
    "perk": ("thinking", "clock"),
    "sleep_breathe": ("sleeping", "clock"),
}
#: Step for the virtual-clock takes, and how many frames three seconds of one is.
CLOCK_STEP_MS = 100
SEQUENCE_MS = 3000
#: How many steps before the event the filming starts, so the run-up is in the sequence too.
EVENT_LEAD_STEPS = 20

BACKGROUNDS = {
    "dark": "background:#121214;",
    "light": "background:#f5f5f7;",
    # A busy desktop is the honest test: a transparent window with a dirty matte shows up here.
    "busy": (
        "background:"
        "radial-gradient(circle at 20% 30%, #3b6ea5 0 18%, transparent 18%),"
        "radial-gradient(circle at 72% 64%, #c2753a 0 22%, transparent 22%),"
        "repeating-linear-gradient(48deg, #1d2a33 0 14px, #2f4552 14px 28px);"
    ),
}


def npm_build() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise RuntimeError("npm is not on PATH")
    subprocess.run([npm, "run", "build"], cwd=str(PET_DIR), check=True)


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(root: Path, port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/pet/", timeout=0.5)
            return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if proc.poll() is not None:
                raise RuntimeError("http.server exited before serving") from None
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("http.server did not come up")


def page_url(port: int, expression: str, *, animate: bool) -> str:
    mode = "animate=1" if animate else "still=1"
    return (
        f"http://127.0.0.1:{port}/pet/?variant={VARIANT}&{mode}"
        f"&expression={expression}&size={SIZE}&lang=de"
    )


def set_background(page: Page, css: str) -> None:
    page.evaluate("css => { document.body.style.cssText = css; }", css)


def wait_for_rig(page: Page) -> None:
    """Block until the rigged canvas is on the page, so nothing is screenshot half-loaded."""
    page.wait_for_selector("canvas.pet-rig-canvas", timeout=15_000)
    page.wait_for_function(
        "() => { const c = document.querySelector('canvas.pet-rig-canvas');"
        " return c && c.width > 0; }",
        timeout=15_000,
    )


def render_states(page: Page, port: int, out: Path) -> None:
    for state in STATES:
        page.goto(page_url(port, state, animate=False))
        wait_for_rig(page)
        for name, css in BACKGROUNDS.items():
            set_background(page, css)
            page.wait_for_timeout(120)
            page.screenshot(path=str(out / f"state-{state}-{name}.png"))


#: Regions of interest for the unprompted clips, as `[top, bottom, left, right]` in window pixels
#: at `SIZE` = 260. The eye boxes are tight on the two eyeballs; the ear box is the left ear tip.
EVENT_REGIONS = {
    "blink": [(58, 72, 131, 146), (57, 71, 167, 182)],
    "ear_flick": [(2, 30, 100, 132)],
}
#: How many clock steps are searched for an unprompted event before giving up.
EVENT_SEARCH_STEPS = 600
#: Luminance below which a pixel counts as eyeball rather than fur, out of 255.
EYE_DARK_LEVEL = 120
#: A blink is found by how much eyeball disappears, which breathing cannot fake: the count has to
#: fall by this fraction. An ear flick is found by how far the ear tip's box moves away from rest,
#: as mean absolute luminance out of 255. Measured: a flick moves that box by about 11, the breath
#: that runs underneath it by under 5.
BLINK_DROP_FRACTION = 0.5
EAR_FLICK_CHANGE = 7.0


def _regions(png_bytes: bytes, boxes: list[tuple[int, int, int, int]]) -> list[np.ndarray]:
    temporary = PET_DIR / "dist" / "_probe.png"
    temporary.write_bytes(png_bytes)
    image = read_rgba(temporary)[:, :, :3].astype(np.float32)
    luminance = 0.2126 * image[:, :, 0] + 0.7152 * image[:, :, 1] + 0.0722 * image[:, :, 2]
    return [luminance[box[0] : box[1], box[2] : box[3]] for box in boxes]


def _eyeball_pixels(patches: list[np.ndarray]) -> float:
    return float(sum(float((patch < EYE_DARK_LEVEL).sum()) for patch in patches))


def _event_signal(png_bytes: bytes, clip: str) -> float:
    """One number per frame that moves when the clip fires and stays put when it does not.

    For a blink that is how much eyeball is visible, which the breathing cannot fake. For an ear
    flick it is the brightness of the ear tip's box, which only the ear swinging changes.
    """
    patches = _regions(png_bytes, EVENT_REGIONS[clip])
    if clip == "blink":
        return _eyeball_pixels(patches)
    return statistics.fmean(float(patch.mean()) for patch in patches)


def _is_event(clip: str, resting: float, extreme: float) -> bool:
    """Is the most extreme frame of a take far enough from its resting value to be the motion?"""
    if clip == "blink":
        return extreme < resting * BLINK_DROP_FRACTION
    return abs(extreme - resting) > EAR_FLICK_CHANGE


def render_sequences(browser: Browser, port: int, out: Path) -> dict[str, dict[str, object]]:
    """Three seconds of each clip, as numbered frames, under Playwright's virtual clock.

    Looping clips start at zero. A blink and an ear flick do not loop — they fire on a jittered
    schedule — so for those the clock is searched until the region they move actually moves, and
    the three seconds are centred on that. Without it a sequence is a coin toss, and a review that
    sometimes contains no blink is worse than no review.
    """
    costs: dict[str, dict[str, object]] = {}
    for clip, (state, how) in CLIP_SEQUENCES.items():
        folder = out / f"clip-{clip}"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)
        if how == "clock":
            costs[clip] = _film_on_clock(browser, port, state, folder)
        else:
            costs[clip] = _film_live(browser, port, state, clip, folder)
    return costs


def _film_on_clock(browser: Browser, port: int, state: str, folder: Path) -> dict[str, object]:
    """Three seconds of a looping clip, stepped by hand so the phase is exactly reproducible."""
    durations: list[float] = []
    with _clock_page(browser, port, state) as page:
        for index in range(SEQUENCE_MS // CLOCK_STEP_MS):
            started = time.perf_counter()
            page.clock.run_for(CLOCK_STEP_MS)
            page.screenshot(path=str(folder / f"frame-{index:03d}.png"))
            durations.append((time.perf_counter() - started) * 1000)
    return {
        "frames": float(len(durations)),
        "timeline": "virtual clock",
        "step_ms": float(CLOCK_STEP_MS),
        "mean_capture_ms": statistics.fmean(durations),
    }


def _film_live(
    browser: Browser, port: int, state: str, clip: str, folder: Path
) -> dict[str, object]:
    """Film a fixed window, then keep the three seconds around the unprompted motion in it.

    Deciding afterwards rather than while filming: a live threshold needs a resting value, and the
    first frame of a take is as likely as any other to be the middle of a blink, which makes the
    threshold either unreachable or immediately true. With the whole take in hand the resting value
    is the median of it and the event is the extreme.
    """
    frames: list[tuple[float, bytes]] = []
    with _live_page(browser, port, state) as page:
        deadline = time.perf_counter() + LIVE_CAPTURE_SECONDS
        while time.perf_counter() < deadline:
            frames.append((time.perf_counter(), page.screenshot()))

    signals = [_event_signal(shot, clip) for _, shot in frames]
    resting = statistics.median(signals)
    distances = [abs(signal - resting) for signal in signals]
    peak = distances.index(max(distances))
    if not _is_event(clip, resting, signals[peak]):
        raise RuntimeError(
            f"{clip} did not fire in {LIVE_CAPTURE_SECONDS:.0f}s of filming - either the ambient "
            "scheduler is not running or its region of interest is in the wrong place"
        )

    start_at = frames[peak][0] - LIVE_LEAD_SECONDS
    window = [frame for frame in frames if start_at <= frame[0] <= start_at + SEQUENCE_MS / 1000]
    for number, (_, shot) in enumerate(window):
        (folder / f"frame-{number:03d}.png").write_bytes(shot)
    gaps = [later - earlier for (earlier, _), (later, _) in zip(window, window[1:], strict=False)]
    return {
        "frames": float(len(window)),
        "timeline": "real time",
        "mean_frame_gap_ms": statistics.fmean(gaps) * 1000 if gaps else 0.0,
        "event_at_frame": float(sum(1 for moment, _ in window if moment < frames[peak][0])),
    }


#: How long a live take films before it looks for the event, and how much run-up it keeps before
#: it. Fifteen seconds comfortably contains a blink (every 4.2-7.8 s) and an ear flick (6-14 s).
LIVE_CAPTURE_SECONDS = 16.0
LIVE_LEAD_SECONDS = 0.4


@contextmanager
def _live_page(browser: Browser, port: int, state: str) -> Iterator[Page]:
    """A page running on the real clock, for filming motion the virtual one is too coarse for."""
    context = browser.new_context(viewport={"width": WINDOW[0], "height": WINDOW[1]})
    page = context.new_page()
    try:
        page.goto(page_url(port, state, animate=True))
        wait_for_rig(page)
        set_background(page, BACKGROUNDS["dark"])
        yield page
    finally:
        context.close()


@contextmanager
def _clock_page(browser: Browser, port: int, state: str) -> Iterator[Page]:
    """A page whose time only moves when this script says so, closed again afterwards."""
    context = browser.new_context(viewport={"width": WINDOW[0], "height": WINDOW[1]})
    context.clock.install(time=0)
    page = context.new_page()
    try:
        page.goto(page_url(port, state, animate=True))
        wait_for_rig(page)
        set_background(page, BACKGROUNDS["dark"])
        yield page
    finally:
        context.close()


#: Wall-clock seconds each CPU sample runs for. Long enough that a 60 Hz loop contributes
#: hundreds of frames, short enough that three samples do not take a coffee break.
CPU_SAMPLE_SECONDS = 5.0
#: Samples per page. The median is reported; one sample picks up whatever else the machine was
#: doing at the time.
CPU_SAMPLES = 3
#: Chrome counters worth reporting. `TaskDuration` is everything the renderer's main thread did;
#: `ScriptDuration` is the JavaScript inside it, which on this page is the rig's own maths.
CPU_COUNTERS = ("TaskDuration", "ScriptDuration")


def _cpu_share(page: Page) -> dict[str, float]:
    """Share of one core this page's main thread uses, per counter.

    One CDP session for the whole measurement: attaching a new one resets what the counters have
    accumulated, which is how this first reported a rig that cost nothing at all.
    """
    client = page.context.new_cdp_session(page)
    try:
        client.send("Performance.enable")

        def read() -> dict[str, float]:
            metrics = client.send("Performance.getMetrics")["metrics"]
            return {metric["name"]: float(metric["value"]) for metric in metrics}

        samples: dict[str, list[float]] = {name: [] for name in CPU_COUNTERS}
        for _ in range(CPU_SAMPLES):
            before = read()
            started = time.perf_counter()
            page.wait_for_timeout(int(CPU_SAMPLE_SECONDS * 1000))
            elapsed = time.perf_counter() - started
            after = read()
            for name in CPU_COUNTERS:
                samples[name].append((after[name] - before[name]) / elapsed)
    finally:
        client.detach()
    return {name: statistics.median(values) for name, values in samples.items()}


def _frames_per_second(page: Page, seconds: float = 2.0) -> float:
    return float(
        page.evaluate(
            """(seconds) => new Promise((resolve) => {
                 let count = 0;
                 const until = performance.now() + seconds * 1000;
                 const tick = () => {
                   count += 1;
                   if (performance.now() < until) requestAnimationFrame(tick);
                   else resolve(count / seconds);
                 };
                 requestAnimationFrame(tick);
               })""",
            seconds,
        )
    )


def measure_cost(page: Page, port: int) -> dict[str, float]:
    """What the rig costs, as a share of one CPU core.

    Chrome's own counters over a fixed wall-clock window, measured twice: with the rig animating,
    and on `?still=1`, where it draws a single frame and then stops. The difference is the rig.

    The two numbers say different things and both belong in a report. `ScriptDuration` is the rig's
    own arithmetic - posing the skeleton, skinning the meshes, issuing the draw calls - and is what
    it would cost anywhere. `TaskDuration` is the whole main thread, and here it also contains
    rasterisation, because headless Chromium draws on SwiftShader in software. A machine with an
    integrated GPU does that part in hardware, off this thread.
    """
    page.goto(page_url(port, "normal", animate=False))
    wait_for_rig(page)
    still = _cpu_share(page)

    page.goto(page_url(port, "normal", animate=True))
    wait_for_rig(page)
    animated = _cpu_share(page)
    return {
        "frames_per_second": _frames_per_second(page),
        "still_page_main_thread_percent": still["TaskDuration"] * 100,
        "rigged_page_main_thread_percent": animated["TaskDuration"] * 100,
        "rig_main_thread_percent": max(0.0, animated["TaskDuration"] - still["TaskDuration"]) * 100,
        "rig_script_percent": max(0.0, animated["ScriptDuration"] - still["ScriptDuration"]) * 100,
        "sample_seconds": CPU_SAMPLE_SECONDS,
        "samples": float(CPU_SAMPLES),
        "note_software_rasteriser": 1.0,
    }


def draw_overlay(out: Path) -> None:
    """Skeleton and mesh over the base texture: the picture the pivots were placed against."""
    rig = json.loads(RIG_JSON.read_text(encoding="utf-8"))
    image = read_rgba(BASE_TEXTURE).astype(np.float32) / 255.0
    size = image.shape[0]
    backdrop = np.full_like(image[:, :, :3], 0.14)
    alpha = image[:, :, 3:4]
    canvas = image[:, :, :3] * alpha + backdrop * (1 - alpha)

    columns, rows = rig["mesh"]["columns"], rig["mesh"]["rows"]
    for index in range(columns + 1):
        x = min(size - 1, int(index / columns * size))
        canvas[:, x] = canvas[:, x] * 0.55 + np.array([0.25, 0.85, 0.95]) * 0.45
    for index in range(rows + 1):
        y = min(size - 1, int(index / rows * size))
        canvas[y, :] = canvas[y, :] * 0.55 + np.array([0.25, 0.85, 0.95]) * 0.45

    pivots = {bone["name"]: bone["pivot"] for bone in rig["bones"]}
    for bone in rig["bones"]:
        if bone["parent"] is None:
            continue
        _draw_line(canvas, pivots[bone["parent"]], bone["pivot"], (1.0, 0.85, 0.2))
    for bone in rig["bones"]:
        colour = (1.0, 0.3, 0.3) if bone.get("channelOnly") else (1.0, 1.0, 1.0)
        _draw_disc(canvas, bone["pivot"], 4, colour)
        _draw_disc(canvas, bone["pivot"], 7, colour, ring=True)

    out_image = np.concatenate(
        [(np.clip(canvas, 0, 1) * 255).astype(np.uint8), np.full((size, size, 1), 255, np.uint8)],
        axis=2,
    )
    write_rgba(out / "overlay-bones.png", out_image)


def _draw_line(
    canvas: np.ndarray, a: list[float], b: list[float], colour: tuple[float, ...]
) -> None:
    size = canvas.shape[0]
    steps = max(2, int(math.dist(a, b) * size))
    for step in range(steps + 1):
        t = step / steps
        x = int((a[0] + (b[0] - a[0]) * t) * size)
        y = int((a[1] + (b[1] - a[1]) * t) * size)
        if 0 <= x < size and 0 <= y < size:
            canvas[y, x] = colour


def _draw_disc(
    canvas: np.ndarray,
    centre: list[float],
    radius: int,
    colour: tuple[float, ...],
    ring: bool = False,
) -> None:
    size = canvas.shape[0]
    cx, cy = int(centre[0] * size), int(centre[1] * size)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            distance = math.hypot(dx, dy)
            if distance > radius or (ring and distance < radius - 1):
                continue
            x, y = cx + dx, cy + dy
            if 0 <= x < size and 0 <= y < size:
                canvas[y, x] = colour


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--out", default=str(PET_DIR / "dist" / "rig-review"))
    parser.add_argument("--overlay-only", action="store_true")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    draw_overlay(out)
    if args.overlay_only:
        LOG.info("overlay written to %s", out / "overlay-bones.png")
        return 0

    if not args.skip_build:
        npm_build()
    # The built app is served from a scratch directory outside the repository, because vite's
    # `base` is `/pet/` and the core mounts it there too: serving `dist` at the root would 404 on
    # every asset, and a copy left inside `ui/pet` would be linted as if it were source.
    scratch = Path(tempfile.mkdtemp(prefix="nox-pet-rig-"))
    shutil.copytree(DIST_DIR, scratch / "pet")

    port = free_port()
    server = start_server(scratch, port)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
            )
            page = browser.new_page(viewport={"width": WINDOW[0], "height": WINDOW[1]})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            render_states(page, port, out)
            cost = measure_cost(page, port)
            sequences = render_sequences(browser, port, out)
            browser.close()
    finally:
        server.terminate()
        shutil.rmtree(scratch, ignore_errors=True)

    report = {"cost": cost, "sequences": sequences, "page_errors": errors}
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s", json.dumps(report, indent=2))
    if errors:
        LOG.error("%d page error(s) - the rig did not render cleanly", len(errors))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
