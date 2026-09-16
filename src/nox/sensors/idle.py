"""Idle/away sensor (Spec v0.5 §3.5, ST-20-06): two-stage away detection (~10/20 min) from
`Win32Probe.idle_seconds()` (Windows' own last-input timestamp - never keystroke/clipboard
content). Updates `user.present`, `user.last_input_at` and, only while transitioning into/out of
an idle/away stage, `user.activity` - it saves whatever activity value was live before going idle
and restores exactly that on resume, so it never clobbers a value another sensor/service set
(e.g. "coding", "playing") while the user was genuinely active.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from nox.core.state import StateManager
from nox.sensors.history import SensorHistoryStore
from nox.sensors.win32 import Win32Probe

Stage = str  # "active" | "idle" | "away"


class IdleSensor:
    def __init__(
        self,
        probe: Win32Probe,
        state: StateManager,
        *,
        history: SensorHistoryStore | None = None,
        poll_interval_s: float = 5.0,
        idle_after_s: float = 600.0,
        away_after_s: float = 1200.0,
        safe_mode: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if away_after_s <= idle_after_s:
            raise ValueError("away_after_s must be greater than idle_after_s")
        self._probe = probe
        self._state = state
        self._history = history
        self._interval = poll_interval_s
        self._idle_after_s = idle_after_s
        self._away_after_s = away_after_s
        self._safe_mode = safe_mode
        self._clock = clock
        self._stage: Stage = "active"
        self._saved_activity: str | None = None
        self._last_input_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def stage(self) -> Stage:
        return self._stage

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sensor-idle")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self._interval)

    async def poll(self) -> Stage:
        if self._safe_mode():
            return self._stage
        idle_s = max(0.0, self._probe.idle_seconds())
        now = self._clock()
        stage: Stage = "active"
        if idle_s >= self._away_after_s:
            stage = "away"
        elif idle_s >= self._idle_after_s:
            stage = "idle"

        last_input_at = now - timedelta(seconds=idle_s)
        if (
            self._last_input_at is None
            or abs((last_input_at - self._last_input_at).total_seconds()) >= 1.0
        ):
            self._last_input_at = last_input_at
            await self._state.update("user.last_input_at", last_input_at, reason="sensor.idle")

        if stage != self._stage:
            if self._stage == "active":  # first step away from active: remember what it was doing
                current = self._state.get("user.activity")
                self._saved_activity = str(current) if current else None
            await self._state.update("user.present", stage != "away", reason="sensor.idle")
            if stage == "active":
                await self._state.update(
                    "user.activity", self._saved_activity or "unknown", reason="sensor.idle"
                )
                self._saved_activity = None
            else:
                await self._state.update("user.activity", stage, reason="sensor.idle")
            self._stage = stage

        if self._history is not None:
            self._history.record(
                "idle", {"idle_s": idle_s, "stage": stage, "present": stage != "away"}
            )
        return stage
