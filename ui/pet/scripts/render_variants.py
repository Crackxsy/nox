"""Render OP-1 pet concept variants to static PNGs (Decision Plan 2026-09-11, OP-1 technical plan).

Builds `ui/pet` (`npm run build`), serves the `dist` output with `python -m http.server` on a free
port (mounted at `/pet/`, matching the vite `base` and the core's real mount point), then opens the
pet app's dev-only still-frame override (`?variant=<id>&expression=<expr>&still=1&size=512`, see
`petState.ts::stillState` / `App.tsx`) in headless Chromium via Playwright for each of the 4 OP-1
creature concepts x the 6 representative states, and screenshots the transparent canvas at
512x512. Also renders one HTML contact sheet per variant and one overall sheet — via Playwright,
not Pillow (not guaranteed to be installed), per the Decision Plan's explicit instruction.

Run with the project venv (Playwright + Chromium are installed there):
    .venv/Scripts/python.exe ui/pet/scripts/render_variants.py [--skip-build]

Output: E:\\Nox\\vault\\16 - Assets\\pet-variants\\<variant>-<expression>.png (24),
        <variant>-sheet.png (4), all-variants.png (1).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PET_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = PET_DIR / "dist"
OUT_DIR = Path(r"E:\Nox\vault\16 - Assets\pet-variants")

CONCEPT_VARIANTS = ["imp", "fox", "owl", "cat"]
EXPRESSIONS = ["normal", "happy", "thinking", "speaking", "privacy", "sleeping"]
SIZE = 512

VARIANT_TITLES = {
    "imp": "imp — kleiner gehörnter Unruhestifter",
    "fox": "fox — fuchsohriger Zwielicht-Geist",
    "owl": "owl — rundlicher Nacht-Uhu",
    "cat": "cat — schlanker Schatten-Kater",
}

TILE_BG = "repeating-conic-gradient(#2a2a33 0% 25%, #232329 0% 50%) 50% / 16px 16px"


def npm_build() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("npm not found on PATH; run `npm install` in ui/pet first if needed")
    subprocess.run([npm, "run", "build"], cwd=str(PET_DIR), check=True)


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_http_server(serve_root: Path, port: int) -> subprocess.Popen[bytes]:
    """`python -m http.server`, rooted so that `/pet/` resolves to the built dist (matches the
    core's real mount point at `/pet`, see IPC Model / shell/logic.py::pet_url)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(serve_root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    url = f"http://127.0.0.1:{port}/pet/"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if proc.poll() is not None:
                raise RuntimeError("http.server exited before it started serving") from None
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"http.server did not start serving {url} in time")


def render_all(base_url: str) -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            # Wide enough to also lay the contact sheets out in a 3-column grid; the still-frame
            # canvas screenshot below crops to the canvas element itself, so the extra width there
            # is harmless.
            page = browser.new_page(viewport={"width": 1000, "height": SIZE + 100})
            for variant in CONCEPT_VARIANTS:
                tiles: list[Path] = []
                for expr in EXPRESSIONS:
                    out_path = OUT_DIR / f"{variant}-{expr}.png"
                    render_still(page, base_url, variant, expr, out_path)
                    produced.append(out_path)
                    tiles.append(out_path)
                sheet_path = OUT_DIR / f"{variant}-sheet.png"
                render_contact_sheet(page, VARIANT_TITLES[variant], tiles, EXPRESSIONS, sheet_path)
                produced.append(sheet_path)
            all_path = OUT_DIR / "all-variants.png"
            render_overall_sheet(page, CONCEPT_VARIANTS, all_path)
            produced.append(all_path)
        finally:
            browser.close()
    return produced


def render_still(page, base_url: str, variant: str, expr: str, out_path: Path) -> None:
    url = f"{base_url}?variant={variant}&expression={expr}&still=1&size={SIZE}"
    page.goto(url, wait_until="networkidle")
    canvas = page.locator("canvas")
    canvas.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(350)  # let one breathing/blink frame settle before the screenshot
    canvas.screenshot(path=str(out_path), omit_background=True)


def render_contact_sheet(
    page, title: str, tiles: list[Path], labels: list[str], out_path: Path
) -> None:
    tile_size = SIZE // 2
    cells = "".join(
        f'<figure style="margin:8px;background:#0b0b12;border-radius:12px;padding:8px;'
        f'text-align:center;display:inline-block;">'
        f'<img src="{t.as_uri()}" width="{tile_size}" height="{tile_size}" '
        f'style="border-radius:8px;background:{TILE_BG};display:block;">'
        f'<figcaption style="color:#c8c8d4;font-size:13px;margin-top:6px;'
        f'font-family:\'Segoe UI\',sans-serif;">{label}</figcaption></figure>'
        for t, label in zip(tiles, labels, strict=True)
    )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{margin:0;padding:24px;background:#1c1c24;font-family:'Segoe UI',sans-serif;}"
        "h1{color:#e8e8f0;font-size:20px;margin:0 0 16px;}"
        ".grid{display:flex;flex-wrap:wrap;max-width:900px;}"
        "</style></head><body>"
        f'<h1>{title}</h1><div class="grid">{cells}</div></body></html>'
    )
    _screenshot_html(page, html, out_path)


def render_overall_sheet(page, variants: list[str], out_path: Path) -> None:
    sections = "".join(
        f'<h2 style="color:#e8e8f0;font-size:16px;font-family:\'Segoe UI\',sans-serif;">'
        f"{VARIANT_TITLES[v]}</h2>"
        f'<img src="{(OUT_DIR / f"{v}-sheet.png").as_uri()}" '
        f'style="max-width:100%;margin-bottom:28px;border-radius:8px;">'
        for v in variants
    )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "body{margin:0;padding:24px;background:#141418;}"
        "</style></head><body>" + sections + "</body></html>"
    )
    _screenshot_html(page, html, out_path)


def _screenshot_html(page, html: str, out_path: Path) -> None:
    tmp = out_path.with_name("_" + out_path.stem + ".html")
    tmp.write_text(html, encoding="utf-8")
    try:
        page.goto(tmp.as_uri(), wait_until="networkidle")
        page.screenshot(path=str(out_path), full_page=True)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    if "--skip-build" not in sys.argv or not DIST_DIR.exists():
        npm_build()
    if not DIST_DIR.exists():
        raise RuntimeError(f"build did not produce {DIST_DIR}")

    with tempfile.TemporaryDirectory(prefix="nox-pet-serve-") as tmp:
        serve_root = Path(tmp)
        shutil.copytree(DIST_DIR, serve_root / "pet")
        port = free_port()
        proc = start_http_server(serve_root, port)
        try:
            produced = render_all(f"http://127.0.0.1:{port}/pet/")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    sys.stdout.write(f"Rendered {len(produced)} files to {OUT_DIR}:\n")
    for p in produced:
        sys.stdout.write(f" - {p}\n")


if __name__ == "__main__":
    main()
