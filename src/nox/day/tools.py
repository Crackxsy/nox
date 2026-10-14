"""The `calendar.*` and `reminder.*` tools: the user's day, as the model can ask for it.

Reading the calendar is `read` risk. Setting or cancelling a reminder is `low`: it changes
nothing outside Nox, is immediately visible on the dashboard and immediately reversible - the
same reasoning the permission table applies to switching a light. The model gives times as
ISO-8601 in local time; the current time and weekday are in its prompt, so "morgen um 9" is its
arithmetic, not ours.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, model_validator

from nox.day.calendar import CalendarEvent, CalendarService, FeedStatus
from nox.day.reminders import MAX_TEXT_CHARS, ReminderRepository, ReminderRow
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

__all__ = ["register_day_tools"]

#: A reminder more than this far in the past is a mistake in the model's arithmetic, not a wish.
_GRACE = timedelta(minutes=1)
#: Furthest ahead a reminder may be set; a year is plenty and catches "2062" typos.
_HORIZON = timedelta(days=366)

Clock = Callable[[], datetime]


class _EmptyInput(BaseModel):
    pass


class UpcomingInput(BaseModel):
    days: int = Field(default=7, ge=1, le=14, description="How many days ahead, today included.")


class ReminderSetInput(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="What to say when the time comes, in the user's words.",
    )
    at: str | None = Field(
        default=None,
        description="Local date-time, ISO-8601 (e.g. 2026-10-15T09:00). Either this or in_minutes.",
    )
    in_minutes: int | None = Field(
        default=None, ge=1, le=60 * 24 * 366, description="Minutes from now instead of `at`."
    )

    @model_validator(mode="after")
    def _one_time(self) -> ReminderSetInput:
        if (self.at is None) == (self.in_minutes is None):
            raise ValueError("give exactly one of `at` or `in_minutes`")
        return self


class ReminderCancelInput(BaseModel):
    id: str = Field(min_length=1, description="The reminder's id, from reminder.list.")


# ---- shaping -------------------------------------------------------------------------------------


def _event(event: CalendarEvent) -> dict[str, Any]:
    return {
        "start": event.start.isoformat(timespec="minutes"),
        "end": event.end.isoformat(timespec="minutes"),
        "title": event.title,
        "location": event.location,
        "all_day": event.all_day,
        "source": event.source,
    }


def _reminder(row: ReminderRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "text": row.text,
        "due_at": row.due_at.astimezone().isoformat(timespec="minutes"),
        "source": row.source,
    }


def _feed(status: FeedStatus) -> dict[str, Any]:
    return {
        "id": status.id,
        "label": status.label,
        "configured": status.configured,
        "error": status.error,
        "count": status.count,
    }


def _calendar_answer(calendar: CalendarService, events: list[CalendarEvent]) -> dict[str, Any]:
    feeds = calendar.status()
    answer: dict[str, Any] = {
        "configured": calendar.configured,
        "events": [_event(e) for e in events],
        "calendars": [_feed(f) for f in feeds],
    }
    if not calendar.configured:
        answer["hint"] = (
            "No calendar is connected. The user can paste a private iCal URL (Google Calendar, "
            "school timetable) under Settings > Integrations > Calendar."
        )
    elif any(f.error for f in feeds if f.configured):
        answer["hint"] = "At least one calendar could not be fetched; the list may be incomplete."
    return answer


def _parse_due(payload: ReminderSetInput, now: datetime) -> datetime:
    if payload.in_minutes is not None:
        return now + timedelta(minutes=payload.in_minutes)
    assert payload.at is not None  # the validator guarantees one of the two
    try:
        due = datetime.fromisoformat(payload.at)
    except ValueError as exc:
        raise ValueError(f"`at` is not an ISO-8601 date-time: {payload.at!r}") from exc
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    return due


# ---- the tools -----------------------------------------------------------------------------------


def make_calendar_today_tool(calendar: CalendarService) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        await calendar.ensure_fresh()
        return _calendar_answer(calendar, calendar.today())

    return ToolSpec(
        name="calendar.today",
        description=(
            "Today's appointments from the user's connected calendars (Google, school). "
            "Says so when no calendar is connected yet."
        ),
        input_model=_EmptyInput,
        risk=Risk.READ,
        side_effects=False,
        local=False,
        handler=handler,
    )


def make_calendar_upcoming_tool(calendar: CalendarService) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = UpcomingInput.model_validate(arguments)
        await calendar.ensure_fresh()
        return _calendar_answer(calendar, calendar.upcoming(payload.days))

    return ToolSpec(
        name="calendar.upcoming",
        description=(
            "The user's appointments for the next days (default 7, today included) "
            "from their connected calendars."
        ),
        input_model=UpcomingInput,
        risk=Risk.READ,
        side_effects=False,
        local=False,
        handler=handler,
    )


def make_reminder_set_tool(repo: ReminderRepository, clock: Clock) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = ReminderSetInput.model_validate(arguments)
        now = clock()
        due = _parse_due(payload, now)
        if due < now - _GRACE:
            raise ValueError(
                f"that time has already passed (now is {now.isoformat('T', 'minutes')})"
            )
        if due > now + _HORIZON:
            raise ValueError("a reminder can be at most a year ahead")
        row = repo.add(text=payload.text, due_at=due, source="tool")
        return {**_reminder(row), "in_minutes": int((due - now).total_seconds() // 60)}

    return ToolSpec(
        name="reminder.set",
        description=(
            "Set a reminder Nox speaks at a local date-time (`at`, ISO-8601) or in N minutes. "
            "Compute `at` from the current time in the prompt."
        ),
        input_model=ReminderSetInput,
        risk=Risk.LOW,
        side_effects=True,
        local=True,
        targets=lambda arguments: str(arguments.get("at") or arguments.get("in_minutes") or ""),
        handler=handler,
    )


def make_reminder_list_tool(repo: ReminderRepository) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        return {"items": [_reminder(r) for r in repo.list_open()]}

    return ToolSpec(
        name="reminder.list",
        description="The user's open reminders, soonest first, with their ids.",
        input_model=_EmptyInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def make_reminder_cancel_tool(repo: ReminderRepository) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        payload = ReminderCancelInput.model_validate(arguments)
        if repo.cancel(payload.id):
            return {"ok": True, "id": payload.id}
        return {"ok": False, "reason": "no open reminder with that id"}

    return ToolSpec(
        name="reminder.cancel",
        description="Cancel one of the user's open reminders by id (see reminder.list).",
        input_model=ReminderCancelInput,
        risk=Risk.LOW,
        side_effects=True,
        local=True,
        targets=lambda arguments: str(arguments.get("id") or ""),
        handler=handler,
    )


def register_day_tools(
    registry: ToolRegistry,
    *,
    calendar: CalendarService,
    reminders: ReminderRepository,
    clock: Clock,
) -> None:
    registry.register(make_calendar_today_tool(calendar))
    registry.register(make_calendar_upcoming_tool(calendar))
    registry.register(make_reminder_set_tool(reminders, clock))
    registry.register(make_reminder_list_tool(reminders))
    registry.register(make_reminder_cancel_tool(reminders))
