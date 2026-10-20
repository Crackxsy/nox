# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright==1.64.0", "psutil==7.2.2", "imageio-ffmpeg==0.6.0", "pillow==13.0.0",
# ]
# ///
"""Record and measure the relief renderer headlessly: one short video per pet state, a tour of
state transitions next to the older rig renderer, and the cost per frame.

Everything runs in headless Chromium - no window opens, no audio plays - against the built pet
served from a scratch copy, with the dev preview flags (`?animate=1&expression=...`, `?tour=...`).
Videos are rendered frame by frame on Playwright's virtual clock, so they are smooth and the same
on every run whatever the machine is doing; the cost measurement runs on the real clock.

    uv run --no-project ui/pet/scripts/relief/record_relief.py --out DIR [--skip-build]
        [--variant eulenfuchs] [--only states,tour,perf] [--seconds 4] [--fps 30]

Output in DIR: `state-<name>.mp4` per state, `tour-relief.mp4` / `tour-rig.mp4` (the same state
tour on the new and the old renderer), `vergleich.mp4` (both side by side), `vergleich.html`,
`metrics.json` (fps, JS ms per frame, CPU, bundle size). Exit code 1 on any page error.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import gzip
import http.server
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import imageio_ffmpeg
import psutil
from PIL import Image
from playwright.sync_api import Browser, Page, sync_playwright

PET = Path(__file__).resolve().parent.parent.parent
DIST = PET / "dist"
SIZE = 520
BACKGROUND = "#1e1e22"  # the dark grey of an OBS scene
EXPRESSIONS = [
    "normal",
    "happy",
    "excited",
    "confused",
    "curious",
    "bored",
    "proud",
    "shy",
    "scared",
    "angry",
    "sad",
    "coding",
    "working",
    "thinking",
    "coaching",
    "streaming",
    "celebrating",
    "sleeping",
    "tilted",
    "hype",
    "smug",
]
FUNCTIONALS = ["listening", "speaking", "error", "muted", "privacy", "unavailable", "offline"]
TOUR = [
    "normal",
    "listening",
    "thinking",
    "speaking",
    "happy",
    "celebrating",
    "curious",
    "sad",
    "scared",
    "feed",
    "normal",
    "bored",
    "sleeping",
    "normal",
    "error",
    "normal",
]
TOUR_EVERY_MS = 3200
#: States that start asleep are recorded falling asleep: a second awake, then the state.
RESTING = {"sleeping", "offline", "unavailable"}
#: gzip budget per lazy relief chunk, bytes: the renderer must stay small.
BUDGET_GZIP = {"ReliefPet": 16_000, "reliefFile": 4_000}
CHROMIUM_ARGS = [
    "--use-angle=d3d11",
    "--ignore-gpu-blocklist",
    "--enable-gpu-rasterization",
    "--mute-audio",
]


def build() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit("npm is not on PATH")
    subprocess.run([npm, "run", "build"], cwd=str(PET), check=True)


@contextlib.contextmanager
def serve(root: Path) -> Iterator[int]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def url(port: int, variant: str, query: str) -> str:
    return f"http://127.0.0.1:{port}/pet/?variant=sprite:{variant}&animate=1&overlay=1&size={SIZE}&lang=en&{query}"


def open_page(browser: Browser, errors: list[str]) -> Page:
    page = browser.new_page(viewport={"width": SIZE, "height": SIZE}, device_scale_factor=1)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    return page


def ready(page: Page, relief: bool) -> None:
    page.wait_for_selector("canvas.pet-rig-canvas", timeout=20_000)
    if relief:
        page.wait_for_function("() => !!window.__noxRelief", timeout=20_000)
    page.evaluate(
        "bg => { for (const el of [document.documentElement, document.body])"
        " el.style.background = bg; }",
        BACKGROUND,
    )


def encoder(path: Path, fps: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-loglevel",
            "error",
            "-y",
            "-f",
            "image2pipe",
            "-framerate",
            str(fps),
            "-i",
            "-",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-threads",
            "2",
            "-movflags",
            "+faststart",
            str(path),
        ],
        stdin=subprocess.PIPE,
    )


def record(page: Page, target: str, path: Path, seconds: float, fps: int, relief: bool) -> None:
    """Frame-by-frame on the virtual clock: advance 1/fps, screenshot, pipe into ffmpeg."""
    page.clock.install(time=0)
    page.goto(target)
    ready(page, relief)
    # Freeze the clock: an installed clock otherwise keeps flowing with real time, and every
    # screenshot would add its own duration to the animation. `pause_at` takes seconds.
    page.clock.pause_at((page.evaluate("Date.now()") + 100) / 1000)
    page.clock.run_for(200)  # first layout + texture upload
    enc = encoder(path, fps)
    assert enc.stdin is not None
    now = 0
    for k in range(1, int(seconds * fps) + 1):
        due = round(k * 1000 / fps)  # whole ms, without drift
        page.clock.run_for(due - now)
        now = due
        enc.stdin.write(
            page.screenshot(type="png", clip={"x": 0, "y": 0, "width": SIZE, "height": SIZE})
        )
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit(f"ffmpeg failed for {path.name}")


def browser_processes() -> list[psutil.Process]:
    procs = []
    for p in psutil.Process().children(recursive=True):
        with contextlib.suppress(psutil.Error):
            if "chrom" in p.name().lower() or "headless" in p.name().lower():
                procs.append(p)
    return procs


def kind_of(p: psutil.Process) -> str:
    with contextlib.suppress(psutil.Error):
        cmd = " ".join(p.cmdline())
        for t in ("renderer", "gpu-process", "utility"):
            if f"--type={t}" in cmd:
                return t
        return "browser"
    return "gone"


def measure(
    page: Page, target: str, relief: bool, warm: float, seconds: float
) -> dict[str, float | str]:
    """Real clock: frames, JS time per frame (relief only), main-thread busy, process CPU."""
    page.goto(target)
    ready(page, relief)
    gl = page.evaluate(
        """() => { const g = document.createElement('canvas').getContext('webgl2');
        const i = g && g.getExtension('WEBGL_debug_renderer_info');
        return i ? g.getParameter(i.UNMASKED_RENDERER_WEBGL) : 'unknown'; }"""
    )
    time.sleep(warm)
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    procs = browser_processes()
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.cpu_percent(None)

    def metrics() -> dict[str, float]:
        return {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}

    frames0 = page.evaluate("() => window.__noxRelief ? window.__noxRelief.frames : 0")
    raf0 = page.evaluate(
        """() => new Promise(r => { let n = 0; const t = performance.now();
        (function f() { if (++n > 30) r((performance.now() - t) / 30);
        else requestAnimationFrame(f); })(); })"""
    )
    m0 = metrics()
    t0 = time.perf_counter()
    time.sleep(seconds)
    m1 = metrics()
    elapsed = time.perf_counter() - t0
    cpu: dict[str, float] = {}
    for p in procs:
        k = kind_of(p)
        with contextlib.suppress(psutil.Error):
            cpu[k] = cpu.get(k, 0.0) + p.cpu_percent(None)
    stats = page.evaluate("() => window.__noxRelief || null")
    out: dict[str, float | str] = {
        "gpu": gl,
        "main_thread_busy_pct": round(100 * (m1["TaskDuration"] - m0["TaskDuration"]) / elapsed, 2),
        "script_pct": round(100 * (m1["ScriptDuration"] - m0["ScriptDuration"]) / elapsed, 2),
        "js_heap_mb": round(m1["JSHeapUsedSize"] / 2**20, 1),
        "raf_interval_ms": round(raf0, 2),
        **{f"cpu_{k}_pct_of_one_core": round(v, 1) for k, v in sorted(cpu.items())},
    }
    if stats:
        js = sorted(stats["jsMs"])
        out["fps"] = round((stats["frames"] - frames0) / elapsed, 1)
        out["js_ms_per_frame_mean"] = round(statistics.fmean(js), 3)
        out["js_ms_per_frame_p95"] = round(js[int(0.95 * (len(js) - 1))], 3)
        out["vertices"] = stats["vertices"]
    cdp.detach()
    return out


#: Hide WebGL2 from the page, as a browser or GPU without it would.
NO_WEBGL2 = """(() => { const get = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
    return type === 'webgl2' ? null : get.call(this, type, ...rest); }; })();"""
FALLBACKS: dict[str, list[str]] = {
    # no WebGL2: the relief renderer steps aside, the older rig draws on WebGL1
    "webgl1": [],
    # no WebGL at all: the older rig draws on Canvas 2D
    "canvas2d": ["--disable-webgl", "--disable-3d-apis"],
}


def check_fallbacks(pw: object, port: int, variant: str, out: Path) -> dict[str, dict[str, object]]:
    """Without WebGL2 (or any WebGL) the pet must still be there, drawn by the older renderer."""
    result: dict[str, dict[str, object]] = {}
    bg = tuple(int(BACKGROUND[i : i + 2], 16) for i in (1, 3, 5))
    for name, flags in FALLBACKS.items():
        browser = pw.chromium.launch(headless=True, args=[*CHROMIUM_ARGS, *flags])  # type: ignore[attr-defined]
        try:
            logs: list[str] = []
            page = browser.new_page(viewport={"width": SIZE, "height": SIZE})
            page.add_init_script(NO_WEBGL2)
            page.on("console", lambda m, logs=logs: logs.append(f"{m.type}: {m.text}"))
            page.goto(url(port, variant, "expression=normal"))
            ready(page, relief=False)
            page.wait_for_timeout(800)
            shot = out / f"fallback-{name}.png"
            page.screenshot(path=str(shot), clip={"x": 0, "y": 0, "width": SIZE, "height": SIZE})
            im = Image.open(shot).convert("RGB")
            far = (max(abs(a - b) for a, b in zip(px, bg, strict=True)) for px in im.getdata())
            drawn = sum(1 for d in far if d > 24)
            result[name] = {
                "relief_active": page.evaluate("() => !!window.__noxRelief"),
                "creature_pixels": drawn,
                "log": [line for line in logs if "relief" in line or "rig_backend" in line],
            }
            print(f"fallback {name}: {result[name]}", flush=True)
        finally:
            browser.close()
    return result


def bundle_sizes() -> dict[str, object]:
    """JS per chunk (raw and gzip -9): the relief renderer is a lazy chunk, only fetched for a
    converted creature, so the main bundle is what every pet pays."""
    chunks = {}
    for p in sorted((DIST / "assets").glob("*.js")):
        data = p.read_bytes()
        chunks[p.name.split("-")[0]] = {"raw": len(data), "gzip": len(gzip.compress(data, 9))}
    assets = {
        d.parent.name: sum(f.stat().st_size for f in d.iterdir())
        for d in sorted((PET / "public" / "variants").glob("*/relief"))
    }
    return {"js_chunks": chunks, "relief_asset_bytes": assets}


VERGLEICH = Path(__file__).with_name("vergleich_template.html")


def write_vergleich(out: Path, states: list[str], name: str) -> None:
    states = [s for s in states if (out / f"state-{s}.mp4").exists()]
    cells = "".join(
        f'<figure><video src="state-{s}.mp4" muted playsinline loop autoplay></video>'
        f"<figcaption>{s}</figcaption></figure>"
        for s in states
    )
    page = (
        VERGLEICH.read_text(encoding="utf-8")
        .replace("__STATES__", cells)
        .replace("__NAME__", name.capitalize())
        .replace("__STATES_HIDDEN__", "" if states else "hidden")
    )
    (out / "vergleich.html").write_text(page, encoding="utf-8")
    a, b = out / "tour-rig.mp4", out / "tour-relief.mp4"
    if a.exists() and b.exists():
        subprocess.run(
            [
                imageio_ffmpeg.get_ffmpeg_exe(),
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(a),
                "-i",
                str(b),
                "-filter_complex",
                "hstack=inputs=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "20",
                "-threads",
                "2",
                str(out / "vergleich.mp4"),
            ],
            check=True,
        )


def write_index(root: Path) -> None:
    """One page with every recorded creature: old renderer left, relief right, synchronised."""
    rows = []
    for d in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
        if not (d / "tour-relief.mp4").exists():
            continue
        name = d.name if d != root else "eulenfuchs"
        rel = "" if d == root else f"{d.name}/"
        rows.append(
            f'<section><h2>{name.capitalize()}</h2><div class="pair">'
            f'<figure><video src="{rel}tour-rig.mp4" muted playsinline loop></video>'
            "<figcaption>vorher: Rig-Renderer</figcaption></figure>"
            f'<figure><video src="{rel}tour-relief.mp4" muted playsinline loop></video>'
            "<figcaption>nachher: Relief 2.5D</figcaption></figure></div>"
            f'<p><a href="{rel}vergleich.html">Einzelseite</a></p></section>'
        )
    page = (
        INDEX_HEAD + "".join(rows) + "<script>const v=[...document.querySelectorAll('video')];"
        "document.getElementById('play').onclick=()=>{const p=v[0].paused;"
        "v.forEach(x=>{x.currentTime=0;p?x.play():x.pause();});};</script></main></body></html>"
    )
    (root / "vergleich-alle.html").write_text(page, encoding="utf-8")
    print(f"wrote {root / 'vergleich-alle.html'} ({len(rows)} creatures)")


INDEX_HEAD = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relief: alle Kreaturen</title>
<style>:root{color-scheme:dark}body{margin:0;padding:24px 16px;background:#141417;color:#e8e8ea;
font:15px/1.5 system-ui,sans-serif}main{max-width:1100px;margin:0 auto}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
figure{margin:0}figcaption,p{color:#9a9aa3;font-size:13px}a{color:#9ab7ff}
video{width:100%;background:#1e1e22;border:1px solid #2c2c33;border-radius:6px;display:block}
button{background:#22222a;color:#e8e8ea;border:1px solid #2c2c33;border-radius:6px;padding:6px 12px}
h2{font-size:16px;margin:28px 0 8px}</style></head><body><main>
<h1>Alle Kreaturen: vorher und nachher</h1>
<p>Gleiche Zustandsfolge je Kreatur, 520 × 520, OBS-Dunkelgrau, headless aufgenommen.</p>
<button id="play">Alle abspielen / Pause</button>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--variant", default="eulenfuchs")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--only", default="states,tour,fallback,perf")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--states", help="comma list instead of all 28 states")
    ap.add_argument("--tour", help="comma list of states for the before/after tour")
    ap.add_argument("--every", type=int, default=TOUR_EVERY_MS, help="ms per tour step")
    ap.add_argument(
        "--index",
        action="store_true",
        help="only write OUT/vergleich-alle.html: every creature recorded under OUT, side by side",
    )
    args = ap.parse_args()
    if args.index:
        write_index(args.out)
        return 0
    only = set(args.only.split(","))
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    if not args.skip_build:
        build()
    scratch = Path(tempfile.mkdtemp(prefix="nox-pet-relief-"))
    shutil.copytree(DIST, scratch / "pet")
    errors: list[str] = []
    metrics: dict[str, object] = {"bundle": bundle_sizes(), "size": SIZE}
    states = args.states.split(",") if args.states else EXPRESSIONS + FUNCTIONALS
    try:
        with serve(scratch) as port, sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                if "states" in only:
                    for s in states:
                        page = open_page(browser, errors)
                        query = f"expression={s}"
                        seconds = args.seconds
                        if s in RESTING:
                            query = f"tour=normal,{','.join([s] * 7)}&every=1000"
                            seconds = max(args.seconds, 6.0)
                        record(
                            page,
                            url(port, args.variant, query),
                            out / f"state-{s}.mp4",
                            seconds,
                            args.fps,
                            True,
                        )
                        page.close()
                        print(f"state {s}: ok", flush=True)
                if "tour" in only:
                    steps = args.tour.split(",") if args.tour else TOUR
                    tour = f"tour={','.join(steps)}&every={args.every}"
                    seconds = len(steps) * args.every / 1000
                    for name, extra, relief in (
                        ("relief", "", True),
                        ("rig", "&renderer=rig", False),
                    ):
                        page = open_page(browser, errors)
                        record(
                            page,
                            url(port, args.variant, tour + extra),
                            out / f"tour-{name}.mp4",
                            seconds,
                            args.fps,
                            relief,
                        )
                        page.close()
                        print(f"tour {name}: ok", flush=True)
                if "fallback" in only:
                    metrics["fallback"] = check_fallbacks(pw, port, args.variant, out)
                if "perf" in only:
                    perf = {}
                    for label, query, relief in (
                        ("relief_idle", "expression=normal", True),
                        ("relief_speaking", "expression=speaking", True),
                        ("relief_celebrating", "expression=celebrating", True),
                        ("rig_idle", "expression=normal&renderer=rig", False),
                    ):
                        page = open_page(browser, errors)
                        perf[label] = measure(
                            page, url(port, args.variant, query), relief, warm=2.0, seconds=10.0
                        )
                        page.close()
                        print(f"perf {label}: {perf[label]}", flush=True)
                    metrics["perf"] = perf
            finally:
                browser.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    write_vergleich(out, EXPRESSIONS + FUNCTIONALS, args.variant)
    metrics["page_errors"] = errors
    chunks = metrics["bundle"]["js_chunks"]  # type: ignore[index]
    over = {
        k: v["gzip"] for k, v in chunks.items() if k in BUDGET_GZIP and v["gzip"] > BUDGET_GZIP[k]
    }
    metrics["over_budget"] = over
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 1 if errors or over else 0


if __name__ == "__main__":
    sys.exit(main())
