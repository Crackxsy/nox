"""Starting presets without being asked: at a time of day, or when a program starts or ends.

The scheduling rule is deliberately coarse. Presets switch lights and modes, where being a few
seconds late is not noticeable, so the loop wakes once a minute on the minute and asks each preset
whether this is its minute. Remembering the last minute it handled is what keeps a preset from
firing twice within it - the alternative, a next-run timestamp per preset, buys accuracy nobody
can perceive and adds state that can drift.

Whether a preset is due is a pure function of the preset and a point in time, so the rule can be
tested without waiting for a clock or moving one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from nox.core.config.presets import WEEKDAYS, PresetConfig, PresetsConfig
from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.presets.engine import PresetRunner

log = get_logger(__name__)

__all__ = ["PresetTriggers", "is_due"]

#: The loop checks once per minute; anything finer would be spent waiting.
_TICK_SECONDS = 60.0


def is_due(preset: PresetConfig, now: datetime) -> bool:
    """Is this the minute `preset` is scheduled for?"""
    if not preset.enabled or not preset.triggers.at:
        return False
    if preset.triggers.at != now.strftime("%H:%M"):
        return False
    days = preset.triggers.days
    return not days or WEEKDAYS[now.weekday()] in days


def _matches_process(configured: str, reported: str) -> bool:
    return bool(configured) and configured.casefold() == reported.casefold()


class PresetTriggers:
    """Owns the schedule loop and the process subscriptions."""

    def __init__(
        self,
        *,
        runner: PresetRunner,
        bus: Any,
        settings: Callable[[], PresetsConfig],
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._runner = runner
        self._bus = bus
        self._settings = settings
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._unsubscribe: list[Callable[[], None]] = []
        #: The last minute the schedule was evaluated for, so a minute is never handled twice.
        self._last_minute = ""

    def start(self) -> None:
        self._unsubscribe = [
            self._bus.subscribe(E.SENSOR_PROCESS_STARTED, self._on_process_started),
            self._bus.subscribe(E.SENSOR_PROCESS_ENDED, self._on_process_ended),
        ]
        self._task = asyncio.create_task(self._loop(), name="nox-preset-schedule")

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._seconds_to_next_minute())
            with contextlib.suppress(Exception):
                await self.tick()

    def _seconds_to_next_minute(self) -> float:
        now = self._clock()
        return _TICK_SECONDS - now.second - now.microsecond / 1_000_000.0

    async def tick(self) -> list[str]:
        """Run whatever is due right now. Returns the ids that ran, for tests and logs."""
        now = self._clock()
        minute = now.strftime("%Y-%m-%d %H:%M")
        if minute == self._last_minute:
            return []
        self._last_minute = minute

        config = self._settings()
        if not config.enabled:
            return []

        due = [preset for preset in config.items if is_due(preset, now)]
        for preset in due:
            await self._activate(preset.id, "schedule")
        return [preset.id for preset in due]

    async def _on_process_started(self, event: Event) -> None:
        await self._on_process(event, started=True)

    async def _on_process_ended(self, event: Event) -> None:
        await self._on_process(event, started=False)

    async def _on_process(self, event: Event, *, started: bool) -> None:
        config = self._settings()
        if not config.enabled:
            return
        reported = str(event.payload.get("process") or "")
        for preset in config.items:
            if not preset.enabled:
                continue
            configured = (
                preset.triggers.on_process_start if started else preset.triggers.on_process_end
            )
            if _matches_process(configured, reported):
                await self._activate(preset.id, "process")

    async def _activate(self, preset_id: str, trigger: str) -> None:
        try:
            await self._runner.activate(preset_id, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 - a bad preset must not stop the loop
            log.warning("presets.trigger_failed", preset=preset_id, error=str(exc))
