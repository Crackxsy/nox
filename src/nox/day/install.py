"""Wiring for the user's day: calendar feeds, reminders, their tools, requests and health check.

The two feed URLs live in the Windows Credential Manager (`nox/calendar/*`), never in a config
file: a private iCal address is a bearer credential for the whole calendar. Reminders fire
through `nox.proactive`, looked up lazily because `core.extensions` is only filled after every
extension has installed - and because a reminder set at 09:00 must still be spoken if the
proactive extension was restarted in between.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

import httpx

from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.day.calendar import CalendarService, CalendarSource
from nox.day.ipc import register_day_ipc
from nox.day.reminders import ReminderRepository, ReminderRow, ReminderScheduler
from nox.day.tools import register_day_tools

log = get_logger(__name__)

__all__ = ["CALENDAR_SECRETS", "DayRuntime", "GOOGLE_URL_SECRET", "SCHOOL_URL_SECRET", "install"]

GOOGLE_URL_SECRET = "nox/calendar/google_ics_url"  # noqa: S105 - a secret *name*, not a value
SCHOOL_URL_SECRET = "nox/calendar/school_ics_url"  # noqa: S105 - a secret *name*, not a value

#: The feeds a user can connect. Labels are what the dashboard and the model show.
SOURCES: tuple[CalendarSource, ...] = (
    CalendarSource("google", "Google Kalender", GOOGLE_URL_SECRET),
    CalendarSource("school", "Stundenplan", SCHOOL_URL_SECRET),
)
CALENDAR_SECRETS: tuple[str, ...] = tuple(source.secret for source in SOURCES)


class _SecurityLike(Protocol):
    secrets: Any
    egress: Any


class _Core(Protocol):
    """The subset of the core this module needs (structural, so a test needs no real core)."""

    config: Any
    db: Any
    security: _SecurityLike
    tool_registry: Any
    registry: Any
    extensions: Any
    health: Any


@dataclass
class DayRuntime:
    calendar: CalendarService
    reminders: ReminderRepository
    scheduler: ReminderScheduler
    refresh_task: asyncio.Task[None] | None = None

    async def stop(self) -> None:
        if self.refresh_task is not None and not self.refresh_task.done():
            self.refresh_task.cancel()
            await asyncio.gather(self.refresh_task, return_exceptions=True)
        await self.scheduler.stop()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def install(core: _Core) -> DayRuntime | None:
    cfg = core.config.day
    if not cfg.enabled:
        log.info("day.disabled")
        return None

    def read_secret(name: str) -> str | None:
        try:
            value = core.security.secrets.get(name)
        except Exception as exc:  # noqa: BLE001 - an unavailable keyring is "no calendar"
            log.warning("day.secret_read_failed", error=type(exc).__name__)
            return None
        return None if value is None else str(value)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        """Every fetch goes through the core's egress guard, like every other outbound request."""
        return cast(httpx.AsyncClient, core.security.egress.client(**kwargs))

    calendar = CalendarService(
        list(SOURCES),
        secret=read_secret,
        client_factory=client_factory,
        refresh_s=cfg.refresh_minutes * 60.0,
        lookahead_days=cfg.lookahead_days,
        clock=_local_now,
    )
    repo = ReminderRepository(core.db)

    async def fire(reminder: ReminderRow) -> None:
        proactive = core.extensions.get("proactive") if core.extensions else None
        text = f"{cfg.reminder_prefix} {reminder.text}".strip()
        if proactive is None:
            log.warning("day.reminder_unspoken", id=reminder.id, reason="proactive not installed")
            return
        await proactive.service.notify(
            "urgent", "reminder", text, context_key=f"reminder:{reminder.id}"
        )

    scheduler = ReminderScheduler(repo, fire=fire, poll_s=cfg.reminder_poll_s)
    register_day_tools(core.tool_registry, calendar=calendar, reminders=repo, clock=_local_now)
    register_day_ipc(core.registry, calendar=calendar, reminders=repo, clock=_local_now)

    runtime = DayRuntime(calendar=calendar, reminders=repo, scheduler=scheduler)
    if cfg.reminders_enabled:
        scheduler.start()
    runtime.refresh_task = asyncio.get_running_loop().create_task(
        calendar.run(), name="nox-calendar-refresh"
    )
    _add_health_check(core, runtime)
    log.info("day.installed", reminders=cfg.reminders_enabled, refresh_minutes=cfg.refresh_minutes)
    return runtime


def _add_health_check(core: _Core, runtime: DayRuntime) -> None:
    """`limited` until a calendar is connected, and while one cannot be fetched - honest, never a
    faked `available`."""
    health = getattr(core, "health", None)
    if health is None:
        return

    async def probe() -> tuple[HealthStatus, str]:
        feeds = runtime.calendar.status()
        configured = [f for f in feeds if f.configured]
        if not configured:
            return HealthStatus.LIMITED, "no calendar connected; reminders work"
        failing = [f for f in configured if f.error]
        if failing:
            return HealthStatus.LIMITED, "; ".join(f"{f.label}: {f.error}" for f in failing)
        return HealthStatus.AVAILABLE, f"{sum(f.count for f in configured)} appointments ahead"

    try:
        health.add_check(Check("day", probe))
    except ValueError:  # the check exists already (a second install in one process, in tests)
        pass
