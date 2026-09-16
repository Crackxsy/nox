"""Foreground window/process sensor and privacy-zone wiring (Spec v0.5 §3.5, ST-20-04, SP-15).

Polls `Win32Probe.foreground()`, emits `sensor.foreground_changed` only when the (title, process)
pair actually changes, updates `user.application`/`user.window_title` in `NoxState`, and calls
`PrivacyService.observe_foreground()` so zone enter/leave takes effect and `privacy.zone_changed`
is published - `PrivacyService` itself owns the zone matcher (`match_zone`) and the event; this
sensor only supplies the raw signal, on the local machine, within one poll cycle (EPIC-20 DoD #2).

SP-15 finding (see the spike note's `Result`/`Decision`): `NoxState` is checkpointed to SQLite, so
writing the raw title to `user.window_title` unconditionally would let a zoned window's title hit
disk between the state write and the zone becoming active. This sensor checks the zone itself
(`PrivacyService.match_zone`, a pure/sync lookup) *before* deciding what to persist: while zoned,
`user.window_title` and the `sensor.foreground_changed` payload carry an empty string - only
`user.application` (the process/app identity, "an app was open") and the zone id ever persist, the
title text never does, in or out of a zone check. `PrivacyService.observe_foreground()` still gets
the real title (it needs it to match), but never stores or forwards it either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from nox.core.events import E, Event, EventBus
from nox.core.state import StateManager
from nox.security.privacy import PrivacyService
from nox.sensors.history import SensorHistoryStore
from nox.sensors.win32 import ForegroundInfo, Win32Probe


class ForegroundSensor:
    def __init__(
        self,
        probe: Win32Probe,
        bus: EventBus,
        state: StateManager,
        privacy: PrivacyService,
        *,
        history: SensorHistoryStore | None = None,
        poll_interval_s: float = 1.0,
        safe_mode: Callable[[], bool] = lambda: False,
    ) -> None:
        self._probe = probe
        self._bus = bus
        self._state = state
        self._privacy = privacy
        self._history = history
        self._interval = poll_interval_s
        self._safe_mode = safe_mode
        self._last: ForegroundInfo | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def last(self) -> ForegroundInfo | None:
        return self._last

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sensor-foreground")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self._interval)

    async def poll(self) -> ForegroundInfo | None:
        """One sampling cycle; also callable directly (tests, `sensors.status.read` warm-up)."""
        if self._safe_mode():
            return self._last
        info = self._probe.foreground()
        if self._history is not None:
            self._history.record("foreground", {"process": info.process_name, "changed": False})
        if (
            self._last is not None
            and info.title == self._last.title
            and (info.process_name == self._last.process_name)
        ):
            return self._last
        self._last = info

        # SP-15: decide what may persist *before* writing anything - `NoxState` is checkpointed to
        # SQLite, so a zoned title must never reach it even for one write. `match_zone` is a pure
        # lookup (no side effects), safe to call ahead of the actual `observe_foreground()`.
        zoned = self._privacy.match_zone(info.title, info.process_name) is not None
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
