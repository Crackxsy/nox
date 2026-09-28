"""Foreground window/process sensor and privacy-zone wiring.

Polls `DesktopProbe.foreground`, emits `sensor.foreground_changed` only when the (title, process)
pair actually changes, updates `user.application`/`user.window_title` in `NoxState`, and calls
`PrivacyService.observe_foreground` so zone enter/leave takes effect and `privacy.zone_changed` is
published - `PrivacyService` itself owns the zone matcher (`match_zone`) and the event; this sensor
only supplies the raw signal, on the local machine, within one poll cycle (DoD). finding (see the
spike note's `Result`/`Decision`): `NoxState` is checkpointed to SQLite, so writing the raw title
to `user.window_title` unconditionally would let a zoned window's title hit disk between the state
write and the zone becoming active. This sensor checks the zone itself
(`PrivacyService.match_zone`, a pure/sync lookup) *before* deciding what to persist: while zoned,
`user.window_title` and the `sensor.foreground_changed` payload carry an empty string - only
`user.application` (the process/app identity, "an app was open") and the zone id ever persist, the
title text never does, in or out of a zone check. `PrivacyService.observe_foreground` still gets
the real title (it needs it to match), but never stores or forwards it either.

A reading whose title could not be observed (`ForegroundInfo.limitation`) goes to
`PrivacyService.observe_foreground_unobservable` instead, which fails closed; the reason is kept in
`limitation` for the `sensors` health check.

`zones_only=True` is the sensor that runs when `sensors.enabled` is false: privacy zones must not
depend on the awareness sensors being on, so it still polls the foreground window - but only to
feed the privacy service. It writes no state, publishes no `sensor.foreground_changed` and keeps
no history; nothing about the user's windows is recorded that the zones do not need.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from nox.core.events import E, Event, EventBus
from nox.core.state import StateManager
from nox.security.privacy import PrivacyService
from nox.sensors.history import SensorHistoryStore
from nox.sensors.probe import DesktopProbe, ForegroundInfo
from nox.util.aio import poll_loop


class ForegroundSensor:
    def __init__(
        self,
        probe: DesktopProbe,
        bus: EventBus,
        state: StateManager,
        privacy: PrivacyService,
        *,
        history: SensorHistoryStore | None = None,
        poll_interval_s: float = 1.0,
        safe_mode: Callable[[], bool] = lambda: False,
        zones_only: bool = False,
    ) -> None:
        self._probe = probe
        self._bus = bus
        self._state = state
        self._privacy = privacy
        self._history = history
        self._interval = poll_interval_s
        self._safe_mode = safe_mode
        self._zones_only = zones_only
        self._last: ForegroundInfo | None = None
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""
        #: Why the latest reading could not see the window title; empty while it could.
        self.limitation = ""

    @property
    def last(self) -> ForegroundInfo | None:
        return self._last

    @property
    def zones_only(self) -> bool:
        """True for the privacy-zone-only sensor that runs while `sensors.enabled` is false."""
        return self._zones_only

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sensor-foreground")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _note_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"

    async def _loop(self) -> None:
        await poll_loop(
            self.poll,
            self._interval,
            name="sensor-foreground",
            on_error=self._note_error,
            on_success=lambda: setattr(self, "last_error", ""),
        )

    async def poll(self) -> ForegroundInfo | None:
        """One sampling cycle; also callable directly (tests, `sensors.status.read` warm-up)."""
        if self._safe_mode():
            return self._last
        # Off the loop: on Linux and macOS the probe runs an external tool.
        info = await asyncio.to_thread(self._probe.foreground)
        self.limitation = info.limitation
        if self._zones_only:
            return await self._observe_zones_only(info)
        if self._history is not None:
            self._history.record("foreground", {"process": info.process_name, "changed": False})
        if self._last is not None and _same_window(info, self._last):
            return self._last
        self._last = info

        #: decide what may persist *before* writing anything - `NoxState` is checkpointed to
        # SQLite, so a zoned title must never reach it even for one write. `match_zone` is a pure
        # lookup (no side effects), safe to call ahead of the actual `observe_foreground()`. An
        # unobservable reading has no title to persist and always ends in a zone (fail closed).
        zoned = info.limitation != "" or (
            self._privacy.match_zone(info.title, info.process_name) is not None
        )
        visible_title = "" if zoned else info.title

        await self._state.update("user.application", info.process_name, reason="sensor.foreground")
        await self._state.update("user.window_title", visible_title, reason="sensor.foreground")
        await self._bus.publish(
            Event(
                name=E.SENSOR_FOREGROUND_CHANGED,
                payload={"process": info.process_name, "title": visible_title},
                source="sensors",
            )
        )
        # PrivacyService owns the zone matcher and publishes `privacy.zone_changed` itself; give
        # it the real title only to match against - it never stores or forwards it either.
        if info.limitation:
            await self._privacy.observe_foreground_unobservable(info.process_name)
        else:
            await self._privacy.observe_foreground(info.title, info.process_name)
        if self._history is not None:
            self._history.record(
                "foreground",
                {
                    "process": info.process_name,
                    "changed": True,
                    "zone_active": self._privacy.active_zone is not None,
                },
            )
        return info

    async def _observe_zones_only(self, info: ForegroundInfo) -> ForegroundInfo:
        """Feed the privacy service and nothing else (see the module docstring)."""
        if self._last is not None and _same_window(info, self._last):
            return self._last
        self._last = info
        if info.limitation:
            await self._privacy.observe_foreground_unobservable(info.process_name)
        else:
            await self._privacy.observe_foreground(info.title, info.process_name)
        return info


def _same_window(a: ForegroundInfo, b: ForegroundInfo) -> bool:
    """Same title, process and observability - a new pid alone is not a new window."""
    return (a.title, a.process_name, a.limitation) == (b.title, b.process_name, b.limitation)
