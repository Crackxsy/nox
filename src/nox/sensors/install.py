"""Wires the PC-awareness sensors (foreground/zone, idle, resources, game process) onto a started
core
and returns a `SensorsRuntime` the caller stops on shutdown.

`sensors.status.read` is registered regardless of `sensors.enabled`; the sensors themselves only
start when the config enables them. Every sensor's poll checks the kill switch each cycle and no-
ops while it is engaged, so safe mode really does stop the observation, not just the reporting. A
sensor whose poll keeps failing is reported by the `sensors` health check as `limited` with the
error - a frozen history is never served as if it were live.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import psutil

from nox.core.events import EventBus, HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.sensors.foreground import ForegroundSensor
from nox.sensors.game import GameProcessSensor, psutil_process_lister
from nox.sensors.history import SensorHistoryStore
from nox.sensors.idle import IdleSensor
from nox.sensors.resources import NvidiaSmiGpuProbe, ResourceSensor
from nox.sensors.tools import make_sensors_status_read_tool
from nox.sensors.win32 import RealWin32Probe, Win32Probe

log = get_logger(__name__)


class _Core(Protocol):
    """The subset of a started core this module needs, declared locally rather than importing the
    composition root - `nox.sensors` must not depend on `nox.app`."""

    config: Any
    bus: EventBus
    state: StateManager
    security: Any
    tool_registry: Any
    health: Any


class _Sensor(Protocol):
    """What the runtime needs from every sensor, whatever it samples."""

    last_error: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


@dataclass
class SensorsRuntime:
    """Handles for the caller: the shared history plus whichever sensors this host supports."""

    history: SensorHistoryStore
    foreground: ForegroundSensor | None = None
    idle: IdleSensor | None = None
    resources: ResourceSensor | None = None
    game: GameProcessSensor | None = None
    start_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def sensors(self) -> list[tuple[str, _Sensor]]:
        named = (
            ("foreground", self.foreground),
            ("idle", self.idle),
            ("resources", self.resources),
            ("game", self.game),
        )
        return [(name, sensor) for name, sensor in named if sensor is not None]

    async def start(self) -> None:
        for _name, sensor in self.sensors():
            await sensor.start()

    async def stop(self) -> None:
        if self.start_task is not None and not self.start_task.done():
            self.start_task.cancel()
        for _name, sensor in self.sensors():
            await sensor.stop()

    async def health(self) -> tuple[HealthStatus, str]:
        running = self.sensors()
        if not running:
            return HealthStatus.UNAVAILABLE, "no sensor runs on this host"
        failing = [f"{name}: {sensor.last_error}" for name, sensor in running if sensor.last_error]
        if failing:
            return HealthStatus.LIMITED, "; ".join(failing)
        return HealthStatus.AVAILABLE, f"{len(running)} sensors polling"


#: Former name of `SensorsRuntime`, kept so existing callers keep working.
SensorsBundle = SensorsRuntime


def install(core: _Core) -> SensorsRuntime:
    """Install the sensors on `core`. Calling twice creates a second set; stop the first one
    (`await runtime.stop`) before reinstalling."""
    cfg = core.config.sensors
    history = SensorHistoryStore(maxlen=cfg.history_len)
    core.tool_registry.register(make_sensors_status_read_tool(history))

    runtime = SensorsRuntime(history=history)
    if not cfg.enabled:
        return runtime

    _build_sensors(core, runtime)
    _register_health_check(core, runtime)
    runtime.start_task = asyncio.get_running_loop().create_task(
        runtime.start(), name="sensors-start"
    )
    runtime.start_task.add_done_callback(_log_start_failure)
    return runtime


def _build_sensors(core: _Core, runtime: SensorsRuntime) -> None:
    cfg: Any = core.config.sensors
    safe_mode = core.security.killswitch.is_engaged

    probe: Win32Probe | None
    try:
        probe = RealWin32Probe()
    except RuntimeError:
        probe = None  # non-Windows dev/test host: no foreground/idle sensor this run

    if probe is not None:
        runtime.foreground = ForegroundSensor(
            probe,
            core.bus,
            core.state,
            core.security.privacy,
            history=runtime.history,
            poll_interval_s=cfg.foreground.poll_interval_s,
            safe_mode=safe_mode,
        )
        runtime.idle = IdleSensor(
            probe,
            core.state,
            history=runtime.history,
            poll_interval_s=cfg.idle.poll_interval_s,
            idle_after_s=cfg.idle.idle_after_s,
            away_after_s=cfg.idle.away_after_s,
            safe_mode=safe_mode,
        )

    runtime.resources = ResourceSensor(
        psutil,
        core.state,
        gpu_probe=NvidiaSmiGpuProbe() if cfg.resources.gpu_enabled else None,
        history=runtime.history,
        poll_interval_s=cfg.resources.poll_interval_s,
        idle_poll_interval_s=cfg.resources.idle_poll_interval_s,
        cpu_high_watermark_pct=cfg.resources.cpu_high_watermark_pct,
        gpu_enabled=cfg.resources.gpu_enabled,
        safe_mode=safe_mode,
    )
    runtime.game = GameProcessSensor(
        psutil_process_lister,
        core.bus,
        process_names=cfg.game.process_names,
        history=runtime.history,
        poll_interval_s=cfg.game.poll_interval_s,
        safe_mode=safe_mode,
    )


def _register_health_check(core: _Core, runtime: SensorsRuntime) -> None:
    try:
        core.health.add_check(Check("sensors", runtime.health))
    except ValueError:  # installed twice on the same core (tests)
        log.debug("sensors.health_check_already_registered")


def _log_start_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("sensors.start_failed", error=f"{type(exc).__name__}: {exc}")


__all__ = ["SensorsBundle", "SensorsRuntime", "install"]
