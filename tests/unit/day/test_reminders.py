"""`nox.day.reminders`: a reminder survives, fires once, and can be withdrawn."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nox.day.reminders import ReminderRepository, ReminderRow, ReminderScheduler

NOW = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ReminderRepository]:
    from nox.data.db import Database

    db = Database(tmp_path / "nox.db")
    db.migrate()
    yield ReminderRepository(db)
    db.close()


def test_a_reminder_is_stored_tidy_and_listed_soonest_first(repo: ReminderRepository) -> None:
    later = repo.add(text="  Zahnarzt   anrufen ", due_at=NOW + timedelta(hours=2), source="tool")
    sooner = repo.add(text="Wasser trinken", due_at=NOW + timedelta(minutes=5))
    assert later.text == "Zahnarzt anrufen" and later.open and later.due_at.tzinfo is not None
    assert [r.id for r in repo.list_open()] == [sooner.id, later.id]
    assert repo.list_due(NOW) == []
    assert [r.id for r in repo.list_due(NOW + timedelta(minutes=5))] == [sooner.id]
    with pytest.raises(ValueError):
        repo.add(text="   ", due_at=NOW)


def test_cancelling_removes_it_from_the_open_list_but_keeps_the_record(
    repo: ReminderRepository,
) -> None:
    row = repo.add(text="Müll raus", due_at=NOW + timedelta(hours=1))
    assert repo.cancel(row.id) and not repo.cancel(row.id)
    assert repo.list_open() == []
    kept = repo.get(row.id)
    assert kept is not None and kept.cancelled_at is not None
    # the dashboard's day view hides cancelled ones
    assert repo.list_between(NOW, NOW + timedelta(days=1)) == []


async def test_the_scheduler_fires_each_due_reminder_exactly_once(repo: ReminderRepository) -> None:
    spoken: list[str] = []

    async def fire(reminder: ReminderRow) -> None:
        spoken.append(reminder.text)

    repo.add(text="Jetzt", due_at=NOW - timedelta(seconds=30))
    repo.add(text="Später", due_at=NOW + timedelta(hours=1))
    scheduler = ReminderScheduler(repo, fire=fire, clock=lambda: NOW)
    assert len(await scheduler.tick()) == 1 and spoken == ["Jetzt"]
    assert await scheduler.tick() == [] and spoken == ["Jetzt"]
    fired = repo.list_between(NOW - timedelta(hours=1), NOW + timedelta(days=1))
    assert [(r.text, r.fired_at is not None) for r in fired] == [("Jetzt", True), ("Später", False)]


async def test_a_failing_callout_is_marked_fired_and_does_not_stop_the_others(
    repo: ReminderRepository,
) -> None:
    calls: list[str] = []

    async def fire(reminder: ReminderRow) -> None:
        calls.append(reminder.text)
        if reminder.text == "kaputt":
            raise RuntimeError("speaker down")

    repo.add(text="kaputt", due_at=NOW - timedelta(minutes=2))
    repo.add(text="heil", due_at=NOW - timedelta(minutes=1))
    scheduler = ReminderScheduler(repo, fire=fire, clock=lambda: NOW)
    assert len(await scheduler.tick()) == 2 and calls == ["kaputt", "heil"]
    assert repo.list_open() == []


async def test_start_and_stop_leave_no_task_behind(repo: ReminderRepository) -> None:
    async def fire(_reminder: ReminderRow) -> None:
        return None

    scheduler = ReminderScheduler(repo, fire=fire, poll_s=0.01)
    scheduler.start()
    await scheduler.stop()
    await scheduler.stop()  # idempotent
