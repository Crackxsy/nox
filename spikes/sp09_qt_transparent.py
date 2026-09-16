"""SP-09: frameless transparent always-on-top QWebEngineView on Windows 11 (ADR-004 validation).

Shows a transparent HTML page with an idle-breathing canvas pet for a few seconds and prints
measurements: transparency (screen pixels behind the window unchanged), click-through
(WindowFromPoint after WS_EX_TRANSPARENT), CPU of the shell + QtWebEngine child processes while
idle, RSS memory, and frame-time jitter as a flicker proxy. Screenshot goes to spikes/out/sp09.png.

Run: .venv/Scripts/python.exe spikes/sp09_qt_transparent.py
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

import psutil
from PySide6.QtCore import QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QWidget

OUT_DIR = Path(__file__).resolve().parent / "out"
WIN_SIZE = 260
SHOW_SECONDS = float(os.environ.get("SP09_SECONDS", "5"))

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent;overflow:hidden;width:100%;height:100%}
canvas{display:block}
</style></head><body><canvas id="c" width="260" height="260"></canvas><script>
const c=document.getElementById('c'),g=c.getContext('2d');
let t0=performance.now(),frames=0,maxDt=0,last=t0;
function draw(now){const dt=now-last;last=now;if(frames>5)maxDt=Math.max(maxDt,dt);frames++;
 const t=(now-t0)/1000;g.clearRect(0,0,260,260);const br=1+0.04*Math.sin(t*2*Math.PI/3.2);
 g.save();g.translate(130,150);g.scale(br,1/br);
 g.fillStyle='#8b7cf6';g.beginPath();g.ellipse(0,0,70,60,0,0,Math.PI*2);g.fill();
 g.fillStyle='#0b0b12';g.beginPath();g.ellipse(-22,-8,9,12,0,0,Math.PI*2);g.ellipse(22,-8,9,12,0,0,Math.PI*2);g.fill();
 g.restore();window.__sp09={frames,maxDt,elapsed:now-t0};requestAnimationFrame(draw);}
requestAnimationFrame(draw);
</script></body></html>"""


def _exstyle(hwnd: int) -> int:
    return ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)


def set_click_through(hwnd: int, enabled: bool) -> None:
    style = _exstyle(hwnd)
    style = style | WS_EX_LAYERED
    style = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def hwnd_at(x: int, y: int) -> int:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    return int(ctypes.windll.user32.WindowFromPoint(POINT(x, y)))


def root_hwnd(hwnd: int) -> int:
    ga_root = 2
    return int(ctypes.windll.user32.GetAncestor(hwnd, ga_root))


def sample_pixels(screen, rect: QRect, points: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    img = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height()).toImage()
    ratio = img.width() / rect.width()
    out = []
    for px, py in points:
        c = QColor(img.pixel(int(px * ratio), int(py * ratio)))
        out.append((c.red(), c.green(), c.blue()))
    return out


def tree_cpu_mem(proc: psutil.Process) -> tuple[float, float]:
    procs = [proc, *proc.children(recursive=True)]
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(2.0)
    cpu = 0.0
    rss = 0.0
    for p in procs:
        try:
            cpu += p.cpu_percent(None)
            rss += p.memory_info().rss
        except psutil.Error:
            pass
    return cpu / (psutil.cpu_count() or 1), rss / (1024 * 1024)


def main() -> int:
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-logging")
    app = QApplication(sys.argv)
    screen = QGuiApplication.primaryScreen()
    geo = screen.availableGeometry()
    cx0, cy0 = geo.center().x() - WIN_SIZE // 2, geo.center().y() - WIN_SIZE // 2
    rect = QRect(cx0, cy0, WIN_SIZE, WIN_SIZE)
    corners = [(6, 6), (WIN_SIZE - 6, 6), (6, WIN_SIZE - 6), (WIN_SIZE - 6, WIN_SIZE - 6)]
    center = (WIN_SIZE // 2, WIN_SIZE // 2 + 20)

    before = sample_pixels(screen, rect, corners + [center])

    win = QWidget()
    win.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
    )
    win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    win.setGeometry(rect)
    view = QWebEngineView(win)
    view.setGeometry(0, 0, WIN_SIZE, WIN_SIZE)
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)
    view.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
    view.setHtml(PAGE, QUrl("http://127.0.0.1/sp09"))
    win.show()

    result: dict[str, object] = {"window": [rect.x(), rect.y(), WIN_SIZE, WIN_SIZE]}
    proc = psutil.Process()

    def measure() -> None:
        hwnd = int(win.winId())
        after = sample_pixels(screen, rect, corners + [center])
        result["corners_before"] = before[:4]
        result["corners_after"] = after[:4]
        result["center_before"] = before[4]
        result["center_after"] = after[4]
        result["transparent_corners"] = before[:4] == after[:4]
        result["pet_visible_at_center"] = before[4] != after[4]
        black_after = all(c == (0, 0, 0) for c in after[:4])
        black_before = all(c == (0, 0, 0) for c in before[:4])
        result["black_background"] = black_after and not black_before

        cpu, rss = tree_cpu_mem(proc)
        result["cpu_percent_idle_tree"] = round(cpu, 2)
        result["cpu_logical_cores"] = psutil.cpu_count()
        result["rss_mb_tree"] = round(rss, 1)
        result["process_count"] = 1 + len(proc.children(recursive=True))

        cx, cy = rect.center().x(), rect.center().y()
        result["hwnd_at_center_before_clickthrough_is_ours"] = root_hwnd(hwnd_at(cx, cy)) == hwnd
        set_click_through(hwnd, True)
        app.processEvents()
        time.sleep(0.2)
        result["hwnd_at_center_after_clickthrough_is_ours"] = root_hwnd(hwnd_at(cx, cy)) == hwnd
        ours_before = result["hwnd_at_center_before_clickthrough_is_ours"]
        ours_after = result["hwnd_at_center_after_clickthrough_is_ours"]
        result["click_through_works"] = bool(ours_before) and not ours_after
        set_click_through(hwnd, False)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        shot = screen.grabWindow(0, rect.x() - 40, rect.y() - 40, WIN_SIZE + 80, WIN_SIZE + 80)
        shot.save(str(OUT_DIR / "sp09.png"), "PNG")
        result["screenshot"] = str(OUT_DIR / "sp09.png")

        def got(v: object) -> None:
            if isinstance(v, str) and v:
                v = json.loads(v)
            if isinstance(v, dict):
                fps = v["frames"] / max(v["elapsed"] / 1000, 0.001)
                result["fps"] = round(fps, 1)
                result["max_frame_gap_ms"] = round(v["maxDt"], 1)
            print(json.dumps(result, indent=2))  # noqa: T201
            app.quit()

        view.page().runJavaScript("JSON.stringify(window.__sp09)", got)

    QTimer.singleShot(int(SHOW_SECONDS * 1000), measure)
    QTimer.singleShot(int(SHOW_SECONDS * 1000) + 8000, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
