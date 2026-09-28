"""Foreground-window and idle probes for Linux (X11) and macOS, built on the desktop's own tools.

Both shell out instead of binding a native API: `xprop`/`xprintidle` on X11, `osascript` (System
Events) and `ioreg` on macOS. Each call is bounded by `TOOL_TIMEOUT_S`, and the sensors call the
probe from a worker thread, so a slow or hung tool never stalls the event loop.

A tool that fails, times out or prints something unexpected yields a reading with a `limitation`
- never an empty title that would read as "nothing sensitive is open". The foreground sensor turns
that into the fail-closed `unobservable` privacy zone.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence

import psutil

from nox.sensors.probe import ForegroundInfo, ProbeUnavailableError

#: Upper bound for one external tool call. A sensor polls every second or so; a tool that takes
#: longer than this is treated as failed for that reading.
TOOL_TIMEOUT_S = 2.0

#: Runs a command and returns its stdout, or None when it failed, timed out or could not start.
Runner = Callable[[Sequence[str]], str | None]


def run_tool(command: Sequence[str]) -> str | None:
    """The default `Runner`: no shell, bounded, stdout only, errors become None."""
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):  # the process may have exited between the two calls
        return ""


# ---- X11 ----------------------------------------------------------------------------------------

_ACTIVE_WINDOW = re.compile(r"window id # (0x[0-9a-fA-F]+)")
_STRING_PROPERTY = re.compile(r'^(_NET_WM_NAME|WM_NAME)\([^)]*\) = "(.*)"$')
_PID_PROPERTY = re.compile(r"^_NET_WM_PID\(CARDINAL\) = (\d+)$")
_CLASS_PROPERTY = re.compile(r'^WM_CLASS\([^)]*\) = "(?:[^"\\]|\\.)*", "((?:[^"\\]|\\.)*)"$')

X11_FAILED = "xprop did not answer: the active window could not be read"


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


class X11Probe:
    """`_NET_ACTIVE_WINDOW` and its `_NET_WM_NAME`/`_NET_WM_PID` via `xprop`; idle via `xprintidle`.

    Needs an EWMH-compliant window manager, which every mainstream X11 desktop is.
    """

    def __init__(self, runner: Runner = run_tool) -> None:
        self._run = runner

    def foreground(self) -> ForegroundInfo:
        root = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if root is None:
            return ForegroundInfo("", "", 0, limitation=X11_FAILED)
        match = _ACTIVE_WINDOW.search(root)
        if match is None:
            return ForegroundInfo("", "", 0, limitation=X11_FAILED)
        window = match.group(1)
        if int(window, 16) == 0:  # nothing focused (the desktop itself)
            return ForegroundInfo("", "", 0)
        props = self._run(
            ["xprop", "-id", window, "_NET_WM_NAME", "WM_NAME", "_NET_WM_PID", "WM_CLASS"]
        )
        if props is None:
            return ForegroundInfo("", "", 0, limitation=X11_FAILED)
        return self._parse_window(props)

    @staticmethod
    def _parse_window(props: str) -> ForegroundInfo:
        titles: dict[str, str] = {}
        pid = 0
        wm_class = ""
        for line in props.splitlines():
            line = line.strip()
            if (name_match := _STRING_PROPERTY.match(line)) is not None:
                titles[name_match.group(1)] = _unescape(name_match.group(2))
            elif (pid_match := _PID_PROPERTY.match(line)) is not None:
                pid = int(pid_match.group(1))
            elif (class_match := _CLASS_PROPERTY.match(line)) is not None:
                wm_class = _unescape(class_match.group(1))
        title = titles.get("_NET_WM_NAME", titles.get("WM_NAME", ""))
        process = _process_name(pid) or wm_class
        return ForegroundInfo(title, process, pid)

    def idle_seconds(self) -> float:
        out = self._run(["xprintidle"])
        if out is None:
            raise ProbeUnavailableError("xprintidle did not answer")
        try:
            return int(out.strip()) / 1000.0
        except ValueError as exc:
            raise ProbeUnavailableError(f"xprintidle printed {out.strip()[:40]!r}") from exc


# ---- macOS --------------------------------------------------------------------------------------

#: Separates the fields of the AppleScript answer; a unit separator never occurs in a window title.
_SEP = "\x1f"

_FRONTMOST_SCRIPT = """
tell application "System Events"
    set p to first application process whose frontmost is true
    set appName to name of p
    set appPid to unix id of p
    set winTitle to ""
    set titleState to "ok"
    try
        if (count of windows of p) > 0 then set winTitle to name of front window of p
    on error
        set titleState to "unreadable"
    end try
    set sep to ASCII character 31
    return appName & sep & appPid & sep & titleState & sep & winTitle
end tell
"""

MAC_PERMISSION = (
    "macOS did not allow reading the active window: grant Nox (or the Python it runs on) "
    "Accessibility access in System Settings > Privacy & Security > Accessibility"
)

MAC_TITLE_UNREADABLE = "the front window's title could not be read"

_HID_IDLE = re.compile(r'"HIDIdleTime" = (\d+)')


class MacProbe:
    """Frontmost app and its front window's title via System Events; idle via `ioreg`.

    Reading another app's window title requires the Accessibility permission. Without it
    `osascript` fails, and every reading carries `MAC_PERMISSION` until the user grants it.
    """

    def __init__(self, runner: Runner = run_tool) -> None:
        self._run = runner

    def foreground(self) -> ForegroundInfo:
        out = self._run(["osascript", "-e", _FRONTMOST_SCRIPT])
        if out is None:
            return ForegroundInfo("", "", 0, limitation=MAC_PERMISSION)
        fields = out.rstrip("\r\n").split(_SEP, 3)
        if len(fields) != 4:
            return ForegroundInfo("", "", 0, limitation=MAC_PERMISSION)
        name, pid_text, title_state, title = fields
        try:
            pid = int(pid_text)
        except ValueError:
            pid = 0
        if title_state != "ok":
            return ForegroundInfo("", name, pid, limitation=MAC_TITLE_UNREADABLE)
        return ForegroundInfo(title, name, pid)

    def idle_seconds(self) -> float:
        out = self._run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])
        if out is None:
            raise ProbeUnavailableError("ioreg did not answer")
        match = _HID_IDLE.search(out)
        if match is None:
            raise ProbeUnavailableError("ioreg reported no HIDIdleTime")
        return int(match.group(1)) / 1_000_000_000.0


__all__ = [
    "MAC_PERMISSION",
    "MAC_TITLE_UNREADABLE",
    "TOOL_TIMEOUT_S",
    "X11_FAILED",
    "MacProbe",
    "Runner",
    "X11Probe",
    "run_tool",
]
