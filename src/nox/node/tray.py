"""The node's tray icon and its stop key (spec C §1.7, §1.8): what Nox does on this device, at a
glance, and a stop that works even when the core is gone.

* Icon and tooltip follow `status.json` of the running node: connected, not connected with Nox
  ("Nicht verbunden mit Nox"), update needed, stopped.
* **Not-Aus hier** (also `Ctrl+Alt+Shift+K` anywhere, where a global key hook is available) writes
  the node's stop file - exactly `nox node stop`: everything on this device stops at once and the
  core is told to stop everywhere. Only this device resumes it (**Hier fortsetzen**).
* **Was Nox hier darf** lists the capability groups this device allowed; changing them stays a
  terminal command (`nox node allow`), on purpose.
* **Voice satellite** (C9): while its microphone is open the icon says so ("Nox hört zu"); the
  menu mutes and unmutes the microphone here (`nox node voice mute`); `Ctrl+Alt+Space` held is
  push-to-talk.

It is a separate process started by `nox node run` (only with a desktop and the `shell` extra's
PySide6; the key needs pynput) and does nothing but read the node's files and write the stop file,
so a crash here never touches the node.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import IO

from nox.node.home import NodeHome
from nox.node.manifest import GROUPS, load_manifest

__all__ = [
    "PTT_KEYS",
    "STOP_KEYS",
    "TrayView",
    "spawn_tray",
    "stop_here",
    "toggle_mute",
    "tray_available",
    "view",
]

#: The stop key (spec C §1.7).
STOP_KEYS = frozenset({"ctrl", "alt", "shift", "k"})
#: Push-to-talk for the voice satellite, held (the core's default key).
PTT_KEYS = frozenset({"ctrl", "alt", "space"})
REFRESH_MS = 1000

_STATES = {
    "connected": ("normal", "Nox: verbunden", "Nox: connected"),
    "update_needed": ("offline", "Nox: Update nötig", "Nox: update needed"),
    "disconnected": ("offline", "Nicht verbunden mit Nox", "Not connected to Nox"),
    "pin_mismatch": ("offline", "Nox: neu koppeln nötig", "Nox: pair again"),
    "removed": ("offline", "Von Nox entfernt", "Removed from Nox"),
}


@dataclass(frozen=True)
class TrayView:
    tint: str  # a `nox.shell.logic.TrayTint` value
    tooltip: str
    stopped_here: bool


def _voice(home: NodeHome) -> dict[str, object]:
    from nox.node.voice import STATE_FILE  # noqa: PLC0415

    try:
        data = json.loads((home.path / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def view(home: NodeHome, *, german: bool = True) -> TrayView:
    """What the icon shows, from the node's own files only."""
    if home.stop_file.exists():
        text = "Nox: hier gestoppt" if german else "Nox: stopped on this device"
        return TrayView("safe_mode", text, True)
    state = str(home.read_status().get("state", "disconnected"))
    tint, de, en = _STATES.get(state, _STATES["disconnected"])
    voice = _voice(home) if state == "connected" else {}
    if voice.get("mic_open"):
        # Seen as well as heard: the microphone is open for Nox right now.
        return TrayView("capturing", "Nox hört zu" if german else "Nox is listening", False)
    if voice.get("running") and voice.get("muted"):
        de, en, tint = f"{de} (Mikrofon stumm)", f"{en} (microphone muted)", "muted"
    return TrayView(tint, de if german else en, False)


def toggle_mute(home: NodeHome) -> bool:
    """The mute switch of the voice satellite, here (only this device unmutes). True: muted."""
    from nox.node.satellite_cli import voice_mute  # noqa: PLC0415
    from nox.node.voice import MUTE_FILE  # noqa: PLC0415

    muted = not (home.path / MUTE_FILE).exists()
    voice_mute(home, muted)
    return muted


def ptt(home: NodeHome, pressed: bool) -> None:
    """Push-to-talk held or released (a file write: safe from the key hook's own thread)."""
    import time  # noqa: PLC0415

    from nox.node.voice import PTT_FILE  # noqa: PLC0415

    marker = home.path / PTT_FILE
    if pressed:
        home.ensure()
        marker.write_text(str(time.time()), encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)


def allowed_here(home: NodeHome, *, german: bool = True) -> str:
    manifest = load_manifest(home.manifest_file)
    labels = [GROUPS[g].label_en for g in GROUPS if manifest.groups.get(g)]
    head = "Nox darf hier:" if german else "Nox may here:"
    hint = (
        "Ändern nur am Gerät: nox node allow/deny <Gruppe>"
        if german
        else "Change it on this device only: nox node allow/deny <group>"
    )
    return f"{head}\n" + ("\n".join(f"- {label}" for label in labels) or "- nichts") + f"\n\n{hint}"


def stop_here(home: NodeHome) -> None:
    """The same as `nox node stop`."""
    home.ensure()
    home.stop_file.write_text("stopped on this device (tray)\n", encoding="utf-8")


def tray_available() -> bool:
    """A desktop and PySide6 - otherwise there is no tray (a headless node needs none)."""
    from nox.node.confirm import has_screen  # noqa: PLC0415

    return has_screen() and find_spec("PySide6") is not None


def spawn_tray() -> subprocess.Popen[bytes] | None:
    """Start the tray next to the node, with the same code (`nox node run` ends it)."""
    if os.environ.get("NOX_NODE_TRAY") == "0" or not tray_available():
        return None
    app = Path(__file__).resolve().parents[2]  # the folder holding `nox/`
    code = (
        "import os, sys; sys.path.insert(0, os.environ['NOX_NODE_APP']); "
        "from nox.node.tray import main; main()"
    )
    from nox.node.update import python_env  # noqa: PLC0415

    # -I: isolated - no current folder on sys.path, no PYTHON* variables, no user site; and the
    # working folder is the app folder itself, never wherever the node was started from.
    return subprocess.Popen(  # noqa: S603 - this interpreter, fixed code
        [sys.executable, "-I", "-c", code],
        cwd=str(app),
        env=python_env(NOX_NODE_APP=str(app), NOX_NODE_PARENT=str(os.getpid())),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )


def single_instance(home: NodeHome) -> IO[bytes] | None:
    """Hold `tray.lock` for as long as this tray runs; None when another tray already does."""
    home.ensure()
    handle = (home.path / "tray.lock").open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def parent_alive(pid_text: str | None) -> bool:
    """The node that started this tray still runs (no parent named: assume yes)."""
    if not pid_text or not pid_text.isdigit():
        return True
    import psutil  # noqa: PLC0415

    return bool(psutil.pid_exists(int(pid_text)))


def main() -> None:  # pragma: no cover - Qt and a desktop; the logic above is tested
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

    from nox.shell.hotkeys import GlobalHotkeys, HotkeyTracker
    from nox.shell.logic import HotkeyAction, TrayTint
    from nox.shell.tray import make_tray_icon

    home = NodeHome()
    lock = single_instance(home)
    if lock is None:
        return  # one tray per node
    parent = os.environ.get("NOX_NODE_PARENT")
    german = not os.environ.get("NOX_LANG", "de").startswith("en")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    tray = QSystemTrayIcon()
    menu = QMenu()
    state_line = QAction("")
    state_line.setEnabled(False)
    stop = QAction(
        "Not-Aus hier (Strg+Alt+Umschalt+K)" if german else "Stop here (Ctrl+Alt+Shift+K)"
    )
    resume = QAction("Hier fortsetzen" if german else "Resume here")
    allowed = QAction("Was Nox hier darf" if german else "What Nox may do here")
    mute = QAction("Mikrofon stumm / an" if german else "Mute / unmute microphone")
    close = QAction("Symbol ausblenden" if german else "Hide icon")
    stop.triggered.connect(lambda: stop_here(home))
    resume.triggered.connect(lambda: home.stop_file.unlink(missing_ok=True))
    allowed.triggered.connect(
        lambda: QMessageBox.information(None, "Nox", allowed_here(home, german=german))
    )
    close.triggered.connect(app.quit)
    mute.triggered.connect(lambda: toggle_mute(home))
    for action in (state_line, None, stop, resume, allowed, mute, None, close):
        if action is None:
            menu.addSeparator()
        else:
            menu.addAction(action)
    tray.setContextMenu(menu)

    def refresh() -> None:
        if not parent_alive(parent):
            app.quit()  # the node is gone: so is its tray
            return
        shown = view(home, german=german)
        tray.setIcon(make_tray_icon(TrayTint(shown.tint)))
        tray.setToolTip(shown.tooltip)
        state_line.setText(shown.tooltip)
        resume.setEnabled(shown.stopped_here)
        stop.setEnabled(not shown.stopped_here)

    def on_key(action: HotkeyAction, pressed: bool) -> None:
        if action == HotkeyAction.KILL and pressed:
            stop_here(home)  # a file write: safe from the hook's own thread
        elif action == HotkeyAction.PTT:
            ptt(home, pressed)

    keys = GlobalHotkeys(
        HotkeyTracker({STOP_KEYS: HotkeyAction.KILL, PTT_KEYS: HotkeyAction.PTT}), on_key
    )
    keys.start()  # False without pynput: the menu entry still works
    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(REFRESH_MS)
    refresh()
    tray.show()
    try:
        app.exec()
    finally:
        keys.stop()
        lock.close()
