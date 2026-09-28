"""The desktop signal every sensor reads - foreground window and idle time - and which probe
supplies it on the running platform.

`DesktopProbe` is a `Protocol` so every sensor takes one by dependency injection and tests use a
plain fake. `select_probe` picks the implementation for the host: Win32 on Windows, X11 tools on a
Linux X11 session, System Events on macOS, and `UnobservableProbe` everywhere the foreground window
cannot be read (a Wayland session, no display at all, an unknown platform).

The one rule that matters for privacy: a reading whose window title could not be observed carries
a non-empty `limitation`. The foreground sensor turns such a reading into the reserved
`unobservable` privacy zone, so capture, screenshots and memory writes stay off - Nox fails closed
instead of treating "cannot see the banking window" as "no banking window".
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NamedTuple, Protocol


class ForegroundInfo(NamedTuple):
    """One foreground reading. Never persisted itself.

    `limitation` is empty when the window title was read (an empty title is then a real, empty
    title). When it is set, the title is unknown and the text says why, in words a user can act on.
    """

    title: str
    process_name: str
    pid: int
    limitation: str = ""


class DesktopProbe(Protocol):
    def foreground(self) -> ForegroundInfo: ...
    def idle_seconds(self) -> float: ...


class ProbeUnavailableError(RuntimeError):
    """The probe cannot produce this signal on this host; the message is the reason."""


class UnobservableProbe:
    """For hosts whose foreground window cannot be read at all.

    Every reading is unobservable with the same reason, so privacy zones fail closed. Idle time is
    not guessed: `idle_seconds` raises, and `select_probe` does not build an idle sensor for it.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def foreground(self) -> ForegroundInfo:
        return ForegroundInfo("", "", 0, limitation=self.reason)

    def idle_seconds(self) -> float:
        raise ProbeUnavailableError(self.reason)


@dataclass(frozen=True)
class ProbeSelection:
    """The probe for this host, plus what it cannot do.

    `idle_limitation` is empty when idle time is measurable; otherwise no idle sensor is built and
    the reason goes into the `sensors` health check instead of a reading that is never true.
    """

    probe: DesktopProbe
    idle_limitation: str = ""


WAYLAND_REASON = (
    "Wayland lets no application read the active window, so privacy zones cannot be detected and "
    "stay closed; log in to an X11 session to use screen capture and memory"
)
NO_DISPLAY_REASON = "no graphical session: the active window cannot be read"


def select_probe(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ProbeSelection:
    """The probe for `platform` (default: this interpreter's), never raising."""
    platform = platform if platform is not None else sys.platform
    environ = environ if environ is not None else os.environ
    which = which if which is not None else shutil.which

    if platform == "win32":
        from nox.sensors.win32 import RealWin32Probe

        return ProbeSelection(RealWin32Probe())

    if platform == "darwin":
        from nox.sensors.posix import MacProbe

        missing = [tool for tool in ("osascript", "ioreg") if which(tool) is None]
        if "osascript" in missing:
            return ProbeSelection(
                UnobservableProbe("osascript not found: the active window cannot be read"),
                idle_limitation="ioreg not found" if "ioreg" in missing else "",
            )
        return ProbeSelection(
            MacProbe(), idle_limitation="ioreg not found" if "ioreg" in missing else ""
        )

    if platform.startswith("linux") or "bsd" in platform:
        session = environ.get("XDG_SESSION_TYPE", "").strip().lower()
        if session == "wayland" or (environ.get("WAYLAND_DISPLAY") and not environ.get("DISPLAY")):
            return ProbeSelection(UnobservableProbe(WAYLAND_REASON), idle_limitation=WAYLAND_REASON)
        if not environ.get("DISPLAY"):
            return ProbeSelection(
                UnobservableProbe(NO_DISPLAY_REASON), idle_limitation=NO_DISPLAY_REASON
            )
        from nox.sensors.posix import X11Probe

        if which("xprop") is None:
            reason = "xprop not found (package x11-utils): the active window cannot be read"
            return ProbeSelection(UnobservableProbe(reason), idle_limitation=reason)
        idle = "" if which("xprintidle") is not None else "xprintidle not installed"
        return ProbeSelection(X11Probe(), idle_limitation=idle)

    return ProbeSelection(
        UnobservableProbe(f"no foreground probe for platform {platform!r}"),
        idle_limitation=f"no idle probe for platform {platform!r}",
    )


__all__ = [
    "NO_DISPLAY_REASON",
    "WAYLAND_REASON",
    "DesktopProbe",
    "ForegroundInfo",
    "ProbeSelection",
    "ProbeUnavailableError",
    "UnobservableProbe",
    "select_probe",
]
