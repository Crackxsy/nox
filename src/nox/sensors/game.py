"""Generic game/process-lifecycle hook (feeds ST-12's `rl` plugin instead of it polling `psutil`
itself). Polls the running-process list for `sensors.game.process_names` (`config/defaults.yaml`,
e.g. `RocketLeague.exe`) and publishes `sensor.process_started` / `sensor.process_ended` on the bus
- observation only; no input synthesis, memory reads or injection (Security Model; this module
never imports anything beyond `psutil.process_iter`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from nox.core.events import E, Event, EventBus
from nox.sensors.history import SensorHistoryStore
from nox.util.aio import poll_loop


@dataclass(frozen=True)
class RunningProcess:
    name: str
    pid: int


class ProcessLister(Protocol):
    def __call__(self) -> Iterable[RunningProcess]: ...


def psutil_process_lister() -> list[RunningProcess]:
    import psutil

    out: list[RunningProcess] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            out.append(RunningProcess(proc.info["name"] or "", proc.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


class GameProcessSensor:
    def __init__(
        self,
        lister: ProcessLister,
        bus: EventBus,
        *,
        process_names: Iterable[str] = (),
        history: SensorHistoryStore | None = None,
        poll_interval_s: float = 5.0,
        safe_mode: Callable[[], bool] = lambda: False,
    ) -> None:
        self._lister = lister
        self._bus = bus
        self._watched = {name.lower() for name in process_names}
        self._history = history
        self._interval = poll_interval_s
        self._safe_mode = safe_mode
        self._running: dict[str, tuple[float, str]] = {}  # lower(name) -> (start time, real name)
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sensor-game")

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
            name="sensor-game",
            on_error=self._note_error,
            on_success=lambda: setattr(self, "last_error", ""),
        )

    async def poll(self) -> None:
        if self._safe_mode() or not self._watched:
            return
        seen: dict[str, RunningProcess] = {}
        for proc in self._lister():
            key = proc.name.lower()
            if key in self._watched:
                seen[key] = proc

        now = monotonic()
        for key, proc in seen.items():
            if key not in self._running:
                self._running[key] = (now, proc.name)
                await self._bus.publish(
                    Event(
                        name=E.SENSOR_PROCESS_STARTED,
                        payload={"process": proc.name, "pid": proc.pid},
                        source="sensors",
                    )
                )
                if self._history is not None:
                    self._history.record("game", {"process": proc.name, "event": "started"})

        for key in list(self._running):
            if key not in seen:
                started_at, real_name = self._running.pop(key)
                duration_s = now - started_at
                await self._bus.publish(
                    Event(
                        name=E.SENSOR_PROCESS_ENDED,
                        payload={"process": real_name, "duration_s": duration_s},
                        source="sensors",
                    )
                )
                if self._history is not None:
                    self._history.record(
                        "game", {"process": real_name, "event": "ended", "duration_s": duration_s}
                    )
