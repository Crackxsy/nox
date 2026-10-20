"""`nox.day.tools`: the model sets a reminder in local time and hears the truth about calendars."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from nox.data.db import Database
from nox.day.calendar import CalendarService, CalendarSource
from nox.day.reminders import ReminderRepository
from nox.day.tools import register_day_tools
from nox.tools.registry import ToolRegistry

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 14, 21, 30, tzinfo=TZ)


@pytest.fixture
def tools(tmp_path: Path) -> Iterator[tuple[ToolRegistry, ReminderRepository]]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    repo = ReminderRepository(db)
    calendar = CalendarService(
        [CalendarSource("google", "Google Kalender", "nox/calendar/google_ics_url")],
        secret=lambda _name: None,
        client_factory=lambda **_kw: None,  # type: ignore[arg-type,return-value]
        clock=lambda: NOW,
    )
    registry = ToolRegistry()
    register_day_tools(registry, calendar=calendar, reminders=repo, clock=lambda: NOW)
    yield registry, repo
    db.close()


async def _call(registry: ToolRegistry, name: str, **arguments: Any) -> dict[str, Any]:
    spec = registry.get(name)
    assert spec is not None, name
    return await spec.handler(arguments)


async def test_the_five_tools_are_registered_with_the_right_risks(
    tools: tuple[ToolRegistry, ReminderRepository],
) -> None:
    registry, _ = tools
    specs = {name: registry.get(name) for name in registry.names()}
    assert set(specs) == {
        "calendar.today",
        "calendar.upcoming",
        "reminder.set",
        "reminder.list",
        "reminder.cancel",
    }
    today, setter = specs["calendar.today"], specs["reminder.set"]
    assert today is not None and setter is not None
    assert today.risk.value == "read"
    assert setter.risk.value == "low" and setter.side_effects


async def test_a_reminder_for_tomorrow_morning_lands_in_local_time(
    tools: tuple[ToolRegistry, ReminderRepository],
) -> None:
    registry, repo = tools
    answer = await _call(registry, "reminder.set", text="Zahnarzt", at="2026-10-15T09:00")
    assert answer["due_at"] == "2026-10-15T09:00+02:00" and answer["in_minutes"] == 690
    stored = repo.list_open()[0]
    assert stored.due_at == datetime(2026, 10, 15, 9, 0, tzinfo=TZ)
    listed = await _call(registry, "reminder.list")
    assert [item["text"] for item in listed["items"]] == ["Zahnarzt"]
    assert (await _call(registry, "reminder.cancel", id=stored.id))["ok"] is True
    assert (await _call(registry, "reminder.cancel", id=stored.id))["ok"] is False


async def test_relative_reminders_and_impossible_times(
    tools: tuple[ToolRegistry, ReminderRepository],
) -> None:
    registry, _ = tools
    soon = await _call(registry, "reminder.set", text="Nudeln", in_minutes=10)
    assert soon["due_at"] == (NOW + timedelta(minutes=10)).isoformat(timespec="minutes")
    with pytest.raises(ValueError, match="already passed"):
        await _call(registry, "reminder.set", text="x", at="2026-10-14T08:00")
    with pytest.raises(ValueError, match="exactly one"):
        await _call(registry, "reminder.set", text="x", at="2026-10-15T09:00", in_minutes=5)
    with pytest.raises(ValueError, match="ISO-8601"):
        await _call(registry, "reminder.set", text="x", at="morgen um 9")
    with pytest.raises(ValueError, match="a year"):
        await _call(registry, "reminder.set", text="x", at="2030-01-01T09:00")


async def test_calendar_tools_say_when_nothing_is_connected(
    tools: tuple[ToolRegistry, ReminderRepository],
) -> None:
    registry, _ = tools
    today = await _call(registry, "calendar.today")
    assert today["configured"] is False and today["events"] == []
    assert "Settings" in today["hint"]
    assert today["calendars"][0]["label"] == "Google Kalender"
    upcoming = await _call(registry, "calendar.upcoming", days=3)
    assert upcoming["events"] == []


def test_a_calendar_answer_is_capped_and_says_how_many_were_left_out() -> None:
    from types import SimpleNamespace

    from nox.day.calendar import CalendarEvent
    from nox.day.tools import MAX_EVENTS_PER_ANSWER, _calendar_answer

    events = [
        CalendarEvent(
            start=NOW + timedelta(minutes=n),
            end=NOW + timedelta(minutes=n + 1),
            title=f"E{n}",
            source="google",
        )
        for n in range(MAX_EVENTS_PER_ANSWER + 7)
    ]
    calendar = SimpleNamespace(status=lambda: [], configured=True)
    answer = _calendar_answer(calendar, events)  # type: ignore[arg-type]
    assert [e["title"] for e in answer["events"]] == [f"E{n}" for n in range(MAX_EVENTS_PER_ANSWER)]
    assert answer["omitted"] == 7
    small = _calendar_answer(calendar, events[:3])  # type: ignore[arg-type]
    assert len(small["events"]) == 3 and "omitted" not in small
