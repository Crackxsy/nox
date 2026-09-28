"""Wires the PC-awareness sensors (foreground/zone, idle, resources, game process) onto a started
core
and returns a `SensorsRuntime` the caller stops on shutdown.

`sensors.status.read` is registered regardless of `sensors.enabled`; the sensors themselves only
start when the config enables them. Every sensor's poll checks the kill switch each cycle and no-
ops while it is engaged, so safe mode really does stop the observation, not just the reporting. A
sensor whose poll keeps failing is reported by the `sensors` health check as `limited` with the
error - a frozen history is never served as if it were live.

The foreground and idle signals come from the probe `nox.sensors.probe.select_probe` picks for the
platform. Where the foreground window cannot be read (Wayland, no display, a missing macOS
permission), the foreground sensor still runs - its readings put privacy into the fail-closed
`unobservable` zone - and the health check says `limited` with the reason. Where idle time cannot
be measured, no idle sensor runs and the health check names what is missing.
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
from nox.sensors.probe import select_probe
from nox.sensors.resources import NvidiaSmiGpuProbe, ResourceSensor
from nox.sensors.tools import make_sensors_status_read_tool

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
    #: Signals this host cannot provide at all, with the reason (e.g. "idle: xprintidle not
    #: installed"). Reported as `limited`, never hidden.
    unsupported: list[str] = field(default_factory=list)
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
        if self.foreground is not None and self.foreground.limitation:
            failing.append(f"foreground: {self.foreground.limitation} (privacy zones fail closed)")
        failing.extend(self.unsupported)
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

    selection = select_probe()
    probe = selection.probe
    # Always built: on a host that cannot see the foreground window, its readings are what put
    # privacy into the fail-closed zone. Without it, zones would silently never activate.
    runtime.foreground = ForegroundSensor(
        probe,
        core.bus,
        core.state,
        core.security.privacy,
        history=runtime.history,
        poll_interval_s=cfg.foreground.poll_interval_s,
        safe_mode=safe_mode,
    )
    if selection.idle_limitation:
        runtime.unsupported.append(f"idle: {selection.idle_limitation}")
    else:
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
