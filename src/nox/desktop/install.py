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

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psutil

from nox.core.config import NoxConfig
from nox.core.events import E, Event
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.desktop.computer_tools import register_computer_tools
from nox.desktop.computer_use import (
    ComputerUse,
    DryRunSender,
    Win32Sender,
    foreground_pid,
    stop_hotkey_vks,
)
from nox.desktop.tools import register_desktop_tools
from nox.desktop.win32 import RealWindowProbe
from nox.security._events import publish_nowait
from nox.security.model import Risk, TemporaryGrant
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["install"]


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    tool_registry: ToolRegistry
    security: Any
    bus: Any


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


#: Set by the benchmark world: computer use runs every guard but sends nothing.
DRY_RUN_ENV = "NOX_COMPUTER_USE_DRY_RUN"

#: Anti-cheat services: while one runs, a game that guards against input is running, whether or
#: not the user put it on the watched list.
ANTI_CHEAT_PROCESSES = frozenset(
    {
        "easyanticheat.exe",
        "easyanticheat_eos.exe",
        "beservice.exe",
        "beservice_x64.exe",
        "vgc.exe",
        "faceitclient.exe",
        "faceitservice.exe",
        "eaanticheat.gameservice.exe",
    }
)


def _running_names() -> set[str]:
    names: set[str] = set()
    for process in psutil.process_iter(["name"]):
        try:
            names.add(str(process.info.get("name") or "").lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, ValueError):
        return ""


def _nox_pids() -> set[int]:
    """This core and every process it started (workers, the shell when it is a child)."""
    me = psutil.Process(os.getpid())
    pids = {me.pid}
    try:
        pids |= {child.pid for child in me.children(recursive=True)}
        parent = me.parent()
        if parent is not None and "nox" in " ".join(parent.cmdline()).lower():
            pids.add(parent.pid)
    except psutil.Error:
        pass
    return pids


def _install_computer_use(core: CoreLike) -> None:
    """The second input-synthesis module (`nox.desktop.computer_use`); off until enabled."""
    settings = core.config.desktop.computer_use
    watched = {name.lower() for name in core.config.sensors.game.process_names}
    engine = core.security.engine

    def game_running() -> bool:
        return bool(_game_pids(core)) or bool(_running_names() & ANTI_CHEAT_PROCESSES)

    def game_in_foreground() -> bool:
        name = _process_name(foreground_pid())
        return name in watched or name in ANTI_CHEAT_PROCESSES

    def audit(action: str, target: str, result: str) -> None:
        core.security.audit.append(
            actor="companion",
            tool="computer",
            action=action,
            target=target,
            decision="allow" if result == "ok" else "deny",
            result=result,
        )

    def notice(active: bool, reason: str) -> None:
        publish_nowait(
            core.bus,
            "desktop.computer_use",
            {"active": active, "reason": reason, "stop": settings.stop_hotkey},
            source="desktop",
        )

    computer = ComputerUse(
        enabled=lambda: bool(core.config.desktop.computer_use.enabled),
        game_running=game_running,
        game_in_foreground=game_in_foreground,
        nox_pids=_nox_pids,
        sender=lambda: (
            DryRunSender()
            if os.environ.get(DRY_RUN_ENV)
            else Win32Sender(stop_hotkey_vks(core.config.desktop.computer_use.stop_hotkey))
        ),
        audit=audit,
        notice=notice,
    )

    def grant(minutes: int) -> str:
        grant_id = f"computer-{uuid.uuid4().hex[:12]}"
        engine.grant_temporary(
            TemporaryGrant(
                grant_id=grant_id,
                scope="session:computer/*",
                max_risk=Risk.MEDIUM,
                expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
                granted_by="user",
            )
        )
        return grant_id

    end = register_computer_tools(
        core.tool_registry,
        computer,
        lambda: core.config.desktop.computer_use,
        grant=grant,
        revoke=engine.revoke,
        unremember=lambda: engine.revoke_scope("session:computer/session_start"),
    )

    async def on_kill(_event: Event) -> None:
        end("kill switch")

    if core.bus is not None:
        core.bus.subscribe(E.SECURITY_KILL_SWITCH, on_kill)


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
    _install_computer_use(core)
    log.info("desktop.installed")
    return None
