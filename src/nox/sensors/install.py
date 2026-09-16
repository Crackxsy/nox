"""Wires the PC Awareness sensors (foreground/zone, idle, resources, game-process) onto an already
booted `NoxCore`, since `src/nox/app.py` is out of scope for this change (small-edits-only rule,
CP-Agents). Call `install(core)` once, after `core.start()` (see `tests/integration/test_sensors.py`
for the pattern this mirrors from `tests/integration/test_stream_core.py`):

    core = NoxCore(cfg, voice=False)
    await core.start()
    install(core)

`sensors.status.read` is registered on `core.tool_registry` regardless of `sensors.enabled`; the
sensors themselves only start when `core.config.sensors.enabled` is true. Every sensor's poll
checks the kill switch (`core.security.killswitch.is_engaged`) each cycle and no-ops while engaged,
matching ENGINEERING.md's "kill switch ... denies all side effects" for the safe-mode case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psutil

from nox.sensors.foreground import ForegroundSensor
from nox.sensors.game import GameProcessSensor, psutil_process_lister
from nox.sensors.history import SensorHistoryStore
from nox.sensors.idle import IdleSensor
from nox.sensors.resources import NvidiaSmiGpuProbe, ResourceSensor
from nox.sensors.tools import make_sensors_status_read_tool
from nox.sensors.win32 import RealWin32Probe, Win32Probe

if TYPE_CHECKING:
    from nox.app import NoxCore


@dataclass
class SensorsBundle:
    """Handles kept on `core.sensors` for tests/dashboards - not a cross-agent contract."""

    history: SensorHistoryStore
    foreground: ForegroundSensor | None = None
    idle: IdleSensor | None = None
    resources: ResourceSensor | None = None
    game: GameProcessSensor | None = None

    async def stop(self) -> None:
        for sensor in (self.foreground, self.idle, self.resources, self.game):
            if sensor is not None:
                await sensor.stop()


def install(core: NoxCore) -> None:
    """Idempotent-ish: calling twice replaces `core.sensors` with a new bundle; stop the previous
    one first (`await core.sensors.stop()`) if you intend to reinstall."""
    cfg = core.config.sensors
    history = SensorHistoryStore(maxlen=cfg.history_len)
    core.tool_registry.register(make_sensors_status_read_tool(history))

    bundle = SensorsBundle(history=history)
    core.sensors = bundle  # type: ignore[attr-defined]

    if not cfg.enabled:
        return

    safe_mode = core.security.killswitch.is_engaged

    probe: Win32Probe | None
    try:
        probe = RealWin32Probe()
    except RuntimeError:
        probe = None  # non-Windows dev/test host: no foreground/idle sensor this run

    if probe is not None:
        bundle.foreground = ForegroundSensor(
            probe,
            core.bus,
            core.state,
            core.security.privacy,
            history=history,
            poll_interval_s=cfg.foreground.poll_interval_s,
            safe_mode=safe_mode,
        )
        bundle.idle = IdleSensor(
            probe,
            core.state,
            history=history,
            poll_interval_s=cfg.idle.poll_interval_s,
            idle_after_s=cfg.idle.idle_after_s,
            away_after_s=cfg.idle.away_after_s,
            safe_mode=safe_mode,
        )

    bundle.resources = ResourceSensor(
        psutil,
        core.state,
        gpu_probe=NvidiaSmiGpuProbe() if cfg.resources.gpu_enabled else None,
        history=history,
        poll_interval_s=cfg.resources.poll_interval_s,
        idle_poll_interval_s=cfg.resources.idle_poll_interval_s,
        cpu_high_watermark_pct=cfg.resources.cpu_high_watermark_pct,
        gpu_enabled=cfg.resources.gpu_enabled,
        safe_mode=safe_mode,
    )
    bundle.game = GameProcessSensor(
        psutil_process_lister,
        core.bus,
        process_names=cfg.game.process_names,
        history=history,
        poll_interval_s=cfg.game.poll_interval_s,
        safe_mode=safe_mode,
    )

    for sensor in (bundle.foreground, bundle.idle, bundle.resources, bundle.game):
        if sensor is not None:
            asyncio.ensure_future(sensor.start())  # noqa: RUF006 - fire-and-forget by design
