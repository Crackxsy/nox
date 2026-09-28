"""The Windows `DesktopProbe`: foreground window/process and system idle time via user32.

`RealWin32Probe` is the only place in this package that touches `ctypes.windll`. It runs with
normal user rights - no elevation is ever requested - and a failing Win32 call degrades to an
empty/zero reading rather than raising. The shared types live in `nox.sensors.probe`; `Win32Probe`
is kept as that protocol's former name.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any

from nox.sensors.probe import DesktopProbe, ForegroundInfo

#: Former name of `DesktopProbe`, from when Windows was the only platform with a probe.
Win32Probe = DesktopProbe


class RealWin32Probe:
    """`user32.GetForegroundWindow`/`GetWindowTextW`/`GetWindowThreadProcessId` for the active
    window, `user32.GetLastInputInfo` for idle time. Windows-only; constructing this on another
    platform raises immediately so the mistake is loud rather than a silent no-op sensor."""

    # Declared here because the assignments below are unreachable for a type checker running on
    # another platform, which would otherwise leave these attributes without a type.
    _user32: Any
    _kernel32: Any
    _LastInputInfo: type[ctypes.Structure]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("RealWin32Probe requires Windows (ctypes.windll)")
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

        class _LastInputInfo(ctypes.Structure):
            _fields_ = (("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint))

        self._LastInputInfo = _LastInputInfo

    def foreground(self) -> ForegroundInfo:
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return ForegroundInfo("", "", 0)
        length = self._user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        pid = ctypes.c_uint(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return ForegroundInfo(title, self._process_name(pid.value), pid.value)

    def idle_seconds(self) -> float:
        info = self._LastInputInfo()
        info.cbSize = ctypes.sizeof(self._LastInputInfo)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick: int = self._kernel32.GetTickCount()
        millis: int = tick - info.dwTime
        if millis < 0:  # GetTickCount wrapped (49.7 days uptime) - treat as "just active"
            return 0.0
        return float(millis) / 1000.0

    @staticmethod
    def _process_name(pid: int) -> str:
        if not pid:
            return ""
        try:
            import psutil

            return psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 - process may have exited between the two calls
            return ""
