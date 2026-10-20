"""The `day.*` requests the dashboard calls: today's appointments and reminders in one answer.

One request rather than two, because the Home page draws them as one list - the user's day does
not care which table an entry came from. Writes go through the same repository as the tools,
so a reminder added here is spoken exactly like one the model set.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo
from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.day.calendar import CalendarService
from nox.day.reminders import MAX_AHEAD, MAX_TEXT_CHARS, ReminderRepository, ReminderRow
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry

log = get_logger(__name__)

__all__ = ["register_day_ipc"]

#: The two user-facing clients, never a plugin, a worker or a phone.
UI_ROLES = ("shell", "dashboard")

#: Open reminders after today that the page lists under "später".
_LATER_LIMIT = 5


class ReminderAdd(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    #: Local ISO-8601 date-time, as the page's date-time field produces it.
    due_at: str


class ReminderCancel(BaseModel):
    id: str = Field(min_length=1)


def _reminder(row: ReminderRow, tz: tzinfo | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "text": row.text,
        "due_at": row.due_at.astimezone(tz).isoformat(timespec="minutes"),
        "fired": row.fired_at is not None,
        "source": row.source,
    }


def register_day_ipc(
    registry: RequestRegistry,
    *,
    calendar: CalendarService,
    reminders: ReminderRepository,
    clock: Callable[[], datetime],
) -> None:
    async def today(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        now = clock()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        await calendar.ensure_fresh(timeout_s=4.0)
        todays = reminders.list_between(start, end)
        later = [r for r in reminders.list_open() if r.due_at >= end][:_LATER_LIMIT]
        return {
            "date": start.date().isoformat(),
            "configured": calendar.configured,
            "events": [
                {
                    "start": e.start.isoformat(timespec="minutes"),
                    "end": e.end.isoformat(timespec="minutes"),
                    "title": e.title,
                    "location": e.location,
                    "all_day": e.all_day,
                    "source": e.source,
                }
                for e in calendar.today()
            ],
            "reminders": [_reminder(r, now.tzinfo) for r in todays],
            "later": [_reminder(r, now.tzinfo) for r in later],
            "calendars": [
                {
                    "id": f.id,
                    "label": f.label,
                    "configured": f.configured,
                    "error": f.error,
                    "count": f.count,
                    "incomplete": f.truncated,
                    "fetched_at": None
                    if f.fetched_at is None
                    else f.fetched_at.isoformat(timespec="minutes"),
                }
                for f in calendar.status()
            ],
        }

    async def add(ctx: RequestContext, payload: ReminderAdd) -> dict[str, Any]:
        try:
            due = datetime.fromisoformat(payload.due_at)
        except ValueError:
            return {"ok": False, "reason": "not a date-time"}
        now = clock()
        if due.tzinfo is None:
            due = due.replace(tzinfo=now.tzinfo)
        if due < now - timedelta(minutes=1):
            return {"ok": False, "reason": "in the past"}
        if due > now + MAX_AHEAD:
            return {"ok": False, "reason": "more than a year ahead"}
        row = reminders.add(text=payload.text, due_at=due, source=ctx.role)
        log.info("day.reminder_added", id=row.id, by=ctx.role)
        return {"ok": True, **_reminder(row, now.tzinfo)}

    async def cancel(ctx: RequestContext, payload: ReminderCancel) -> dict[str, Any]:
        ok = reminders.cancel(payload.id)
        log.info("day.reminder_cancelled", id=payload.id, by=ctx.role, ok=ok)
        return {"ok": ok}

    registry.register("day.today", EmptyPayload, today, roles=UI_ROLES)
    registry.register("day.reminder.add", ReminderAdd, add, roles=UI_ROLES)
    registry.register("day.reminder.cancel", ReminderCancel, cancel, roles=UI_ROLES)
