"""HealthService: periodic capability probes -> `health.report` / `system.health_changed` (Runtime
Lifecycle 9).

Each `Check` is a named async probe returning `(HealthStatus, reason)` with its own timeout; a
timeout or exception is reported honestly as `unavailable` (never faked as available). Status
changes are emitted as `system.health_changed`, every run emits `health.report` (payload
`HealthReport`), and changes (plus the first observation of a component) are persisted into
`HealthHistoryRepository`.
Optionally mirrors the component map into `NoxState.system.health` through a `StateManager`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from nox.core.events import E, Event, EventBus, HealthChanged, HealthReport, HealthStatus
from nox.core.logging import get_logger
from nox.core.state import StateManager
from nox.data.repos import HealthHistoryRepository

Probe = Callable[[], Awaitable[tuple[HealthStatus, str]]]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    probe: Probe
    timeout_s: float = 5.0


class HealthService:
    def __init__(
        self,
        bus: EventBus,
        repo: HealthHistoryRepository | None = None,
        checks: Iterable[Check] = (),
        *,
        interval_s: float = 30.0,
        state_manager: StateManager | None = None,
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._checks: dict[str, Check] = {}
        for check in checks:
            self.add_check(check)
        self._interval = interval_s
        self._state_manager = state_manager
        self._log = get_logger(__name__)
        self._current: dict[str, HealthChanged] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._runs = 0

    # ---- configuration -----------------------------------------------------------------------

    def add_check(self, check: Check) -> None:
        if check.name in self._checks:
            raise ValueError(f"duplicate health check {check.name!r}")
        self._checks[check.name] = check

    def remove_check(self, name: str) -> None:
        self._checks.pop(name, None)
        self._current.pop(name, None)

    def current(self) -> dict[str, HealthChanged]:
        return dict(self._current)

    @property
    def runs(self) -> int:
        return self._runs

    # ---- probing -----------------------------------------------------------------------------

    async def _probe(self, check: Check) -> HealthChanged:
        try:
            status, reason = await asyncio.wait_for(check.probe(), check.timeout_s)
        except TimeoutError:
            status, reason = HealthStatus.UNAVAILABLE, f"timeout after {check.timeout_s:g}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken probe is a health finding, not a crash
            status, reason = HealthStatus.UNAVAILABLE, f"probe error: {type(exc).__name__}: {exc}"
        return HealthChanged(component=check.name, status=status, reason=reason)

    async def run_once(self) -> HealthReport:
        checks = list(self._checks.values())
        results = await asyncio.gather(*(self._probe(c) for c in checks))
        changed: list[HealthChanged] = []
        for result in results:
            previous = self._current.get(result.component)
            if previous is None or previous.status != result.status:
                changed.append(result)
            self._current[result.component] = result
        self._runs += 1

        for entry in changed:
            if self._repo is not None:
                try:
                    self._repo.add(entry.component, entry.status, entry.reason)
                except Exception as exc:  # noqa: BLE001 - persistence failure must not stop health
                    self._log.error("health.persist_failed", error=str(exc))
            self._log.info(
                "health.changed",
                component=entry.component,
                status=entry.status,
                reason=entry.reason,
            )
            await self._bus.publish(
                Event(
                    name=E.SYSTEM_HEALTH_CHANGED,
                    payload=entry.model_dump(mode="json"),
                    source="health",
                )
            )

        if changed and self._state_manager is not None:
            await self._state_manager.update(
                "system.health",
                {k: v.status.value for k, v in self._current.items()},
                reason="health",
            )

        report = HealthReport(components=dict(self._current))
        await self._bus.publish(
            Event(name=E.HEALTH_REPORT, payload=report.model_dump(mode="json"), source="health")
        )
        return report

    # ---- periodic loop -----------------------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._loop(), name="nox-health")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; the failure is logged
                self._log.error("health.run_failed", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self._interval)
