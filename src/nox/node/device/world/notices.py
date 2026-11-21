"""What the device tells the person at it when a capture or a computer-use session starts or ends,
and the marker file that says a session is on."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from nox.core.logging import get_logger
from nox.node.device.cli_text import language

log = get_logger(__name__)


#: What the notice on this device says before a picture is taken.
CAPTURE_NOTICE = {
    "screen": "Nox nimmt gerade ein Bildschirmfoto auf (vom Core angefragt).",
    "camera": "Nox nimmt gerade ein Kamerabild auf (vom Core angefragt).",
}


CAPTURE_NOTICE_EN = {
    "screen": "Nox is taking a screenshot now (asked for by the Nox at home).",
    "camera": "Nox is taking a camera picture now (asked for by the Nox at home).",
}


COMPUTER_NOTICE_ON = "Nox steuert jetzt Maus und Tastatur auf diesem Gerät. {0} beendet sofort."


COMPUTER_NOTICE_OFF = "Nox steuert Maus und Tastatur nicht mehr."


COMPUTER_NOTICE_ON_EN = "Nox is now controlling mouse and keyboard on this device. {0} stops it."


COMPUTER_NOTICE_OFF_EN = "Nox no longer controls mouse and keyboard."


#: In the node's folder while a computer-use session runs here (`World.mark_session`).
SESSION_MARKER = "computer-session.json"


def _german() -> bool:

    return language() == "de"


def capture_notice(kind: str) -> str:
    return (CAPTURE_NOTICE if _german() else CAPTURE_NOTICE_EN)[kind]


def computer_notice(active: bool, stop_hotkey: str) -> str:
    if _german():
        return COMPUTER_NOTICE_ON.format(stop_hotkey) if active else COMPUTER_NOTICE_OFF
    return COMPUTER_NOTICE_ON_EN.format(stop_hotkey) if active else COMPUTER_NOTICE_OFF_EN


def write_session_marker(path: Path, active: bool, *, minutes: int) -> None:
    """The marker of a running session, or none: written before anyone is told it started,
    removed when it ended. A marker that cannot be written leaves the old one (fail closed)."""
    if not active:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    now = time.time()
    record = {"since": now, "until": now + max(minutes, 1) * 60.0, "pid": os.getpid()}
    try:
        path.write_text(json.dumps(record), encoding="utf-8")
    except OSError as exc:
        log.error("node.session_marker_failed", error=type(exc).__name__)


def session_marker_state(home: Path, *, now: float | None = None) -> str | None:
    """`running` while the marker says a session runs, `unreadable` when it exists and cannot be
    read (fail closed), None without one - or once the longest the session could last is over
    (a node that crashed during a session; the next start removes the marker)."""
    path = home / SESSION_MARKER
    try:
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable"
    until = record.get("until") if isinstance(record, dict) else None
    if not isinstance(until, int | float) or isinstance(until, bool):
        return "unreadable"
    current = time.time() if now is None else now
    return "running" if current < float(until) else None


def clear_session_marker(home: Path) -> None:
    """At the start of `run`: no session outlives the node's process."""
    with contextlib.suppress(OSError):
        (home / SESSION_MARKER).unlink(missing_ok=True)
