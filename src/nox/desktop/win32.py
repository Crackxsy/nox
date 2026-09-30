"""The window layer: enumerate top-level windows, and the four things Nox may do to one.

Deliberately four and no more. `focus`, `minimize`, `restore` and `close` - and `close` posts
`WM_CLOSE`, which is what the X button does: the program is *asked* to close and may still put up
"save your changes?". Nothing here synthesises input. A general "post any message to any window"
tool would be input synthesis wearing a different hat, and the Rocket League boundary forbids that
across the whole source tree.

`SetForegroundWindow` is the one call that regularly fails for reasons outside this code: Windows
refuses it when the calling process does not own the foreground and no user input has happened
recently. That is a feature of the OS, not a bug here, so `focus` reports whether the window
actually ended up in front rather than whether the call returned.
"""

from __future__ import annotations

import ctypes
import sys
from typing import NamedTuple, Protocol

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["RealWindowProbe", "WindowInfo", "WindowProbe"]

SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010


class WindowInfo(NamedTuple):
    """One top-level window, as the tools report it."""

    handle: int
    title: str
    process_name: str
    pid: int
    minimized: bool


class WindowProbe(Protocol):
    """Injected everywhere, so tests use a plain fake and never the real ctypes calls."""

    def windows(self) -> list[WindowInfo]: ...
    def focus(self, handle: int) -> bool: ...
    def minimize(self, handle: int) -> bool: ...
    def restore(self, handle: int) -> bool: ...
    def close(self, handle: int) -> bool: ...


def _process_name(pid: int) -> str:
    try:
        import psutil

        return str(psutil.Process(pid).name())
    except Exception:  # noqa: BLE001 - a process that ended between enumerate and read
        return ""


class RealWindowProbe:
    """The only place in this package that touches `ctypes.windll`.

    Constructing it off Windows raises immediately, so the mistake is loud instead of a sensor that
    silently reports nothing.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("RealWindowProbe requires Windows (ctypes.windll)")
        self._user32 = ctypes.windll.user32

    def windows(self) -> list[WindowInfo]:
        """Visible top-level windows that have a title - what a person would call "a window"."""
        found: list[WindowInfo] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))

        def collect(handle: int, _param: object) -> bool:
            if not self._user32.IsWindowVisible(handle):
                return True
            length = self._user32.GetWindowTextLengthW(handle)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(handle, buffer, length + 1)
            pid = ctypes.c_uint(0)
            self._user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            found.append(
                WindowInfo(
                    handle=int(handle),
                    title=buffer.value or "",
                    process_name=_process_name(pid.value),
                    pid=int(pid.value),
                    minimized=bool(self._user32.IsIconic(handle)),
                )
            )
            return True

        self._user32.EnumWindows(enum_proc(collect), None)
        return found

    def focus(self, handle: int) -> bool:
        """Bring a window to the front, and report whether it is actually there afterwards.

        Windows refuses `SetForegroundWindow` in cases this process cannot influence. Reporting the
        call's return value would mean telling the user a window is in front when it is not.
        """
        if self._user32.IsIconic(handle):
            self._user32.ShowWindow(handle, SW_RESTORE)
        self._user32.SetForegroundWindow(handle)
        return int(self._user32.GetForegroundWindow()) == int(handle)

    def minimize(self, handle: int) -> bool:
        self._user32.ShowWindow(handle, SW_MINIMIZE)
        return bool(self._user32.IsIconic(handle))

    def restore(self, handle: int) -> bool:
        self._user32.ShowWindow(handle, SW_RESTORE)
        return not bool(self._user32.IsIconic(handle))

    def close(self, handle: int) -> bool:
        """Ask the program to close, the way the X button does. It may refuse, or ask to save.

        `PostMessageW` returns as soon as the message is queued, so a true here means "asked", not
        "gone" - and the tool says exactly that.
        """
        return bool(self._user32.PostMessageW(handle, WM_CLOSE, 0, 0))
