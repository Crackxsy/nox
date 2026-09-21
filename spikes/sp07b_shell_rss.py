"""Measure the shell process-tree RSS before/after the hardened QtWebEngine profile - verify the
number before deciding between settings, rather than guessing.

The hardened-profile changes: a single off-the-record `QWebEngineProfile` shared by the pet view
instead of Qt's default profile (no persistent cache/cookies/local storage/service-worker state),
spellcheck and browser plugins disabled, `ScreenCaptureEnabled` and `ShowScrollBars` off, plus an
opt-in `NOX_PET_SOFTWARE_RENDER=1` switch that disables GPU compositing
(`QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu-compositing`), see `src/nox/shell/pet_window.py`.

This spike launches the real shell (`python -m nox.shell --runtime <empty tmp dir>`) twice -- no
core is reachable, so the pet shows the honest offline page while tray, hotkeys and the QtWebEngine
helper processes all start normally, which is the real process tree ADR-004's ~150 MB estimate
refers to:

  1. "before": default settings (GPU compositing on, hardened off-the-record profile).
  2. "after":  `NOX_PET_SOFTWARE_RENDER=1` (software compositing) on top of the same profile.

Each run idles `IDLE_SECONDS` after a startup grace period, then sums private RSS (psutil
`memory_info().rss`) over the shell process and every child (QtWebEngine spawns several helper
processes; RSS is not simply additive across them, but summing is the same approximation the
transparent-window spike already uses and is good enough to compare the two settings against
each other and against a roughly 500 MB whole-system idle budget).

Run: .venv/Scripts/python.exe spikes/sp07b_shell_rss.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

IDLE_SECONDS = float(os.environ.get("SP07B_IDLE_SECONDS", "10"))
STARTUP_GRACE_S = 5.0
REPO_ROOT = Path(__file__).resolve().parents[1]


def _tree_rss_mb(pid: int) -> float:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0.0
    procs = [proc, *proc.children(recursive=True)]
    total = 0.0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total / (1024 * 1024)


def _run_once(*, software_render: bool) -> float:
    runtime_dir = Path(tempfile.mkdtemp(prefix="nox-sp07b-"))
    env = dict(os.environ)
    if software_render:
        env["NOX_PET_SOFTWARE_RENDER"] = "1"
    else:
        env.pop("NOX_PET_SOFTWARE_RENDER", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "nox.shell", "--runtime", str(runtime_dir)],
        env=env,
        cwd=str(REPO_ROOT),
    )
    try:
        time.sleep(STARTUP_GRACE_S)
        if proc.poll() is not None:
            raise RuntimeError(f"shell exited early with code {proc.returncode}")
        time.sleep(IDLE_SECONDS)
        return _tree_rss_mb(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def main() -> int:
    print(f"idling {IDLE_SECONDS:.0f}s per run after a {STARTUP_GRACE_S:.0f}s startup grace ...")

    print("run 1/2: default settings (GPU compositing, hardened profile) ...")
    before = _run_once(software_render=False)
    print(f"before (GPU compositing): {before:.1f} MB")

    print("run 2/2: NOX_PET_SOFTWARE_RENDER=1 (software compositing) ...")
    after = _run_once(software_render=True)
    print(f"after  (software compositing): {after:.1f} MB")

    print(f"\nRESULT before={before:.1f} MB after={after:.1f} MB delta={before - after:+.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
