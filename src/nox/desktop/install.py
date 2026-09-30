"""`install(core)`: wires the desktop tools onto a started core.

Returns `None`: nothing to shut down, and nothing to stop polling - every tool here reads the
machine when it is called rather than keeping a picture of it.

Which processes count as "the game" is resolved per call from `sensors.game.process_names`, the same
list the game sensor watches. Reading the config rather than the sensor's state is deliberate: the
answer is then current even if the sensor is off, and the boundary that keeps Nox's hands off the
game must not depend on another component being alive.

Off Windows the tools are not registered at all. A window tool that always answers "nothing found"
would be worse than an honest gap, and the capability catalogue already has a word for that.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol

import psutil

from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.desktop.tools import register_desktop_tools
from nox.desktop.win32 import RealWindowProbe
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["install"]


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    tool_registry: ToolRegistry


def _game_pids(core: CoreLike) -> set[int]:
    """Process ids of anything on the watched-game list that is running right now."""
    watched = {name.lower() for name in core.config.sensors.game.process_names}
    if not watched:
        return set()
    pids: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if str(process.info.get("name") or "").lower() in watched:
                pids.add(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def install(core: CoreLike) -> ExtensionRuntime:
    if sys.platform != "win32":  # pragma: no cover - the target platform is Windows
        log.info("desktop.not_windows", detail="no desktop tools registered")
        return None
    try:
        probe: Any = RealWindowProbe()
    except RuntimeError as exc:  # pragma: no cover - a Windows without user32
        log.warning("desktop.probe_unavailable", error=str(exc))
        return None

    register_desktop_tools(core.tool_registry, probe, lambda: _game_pids(core))
    log.info("desktop.installed")
    return None
