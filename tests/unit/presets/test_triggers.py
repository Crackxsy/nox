"""Presets that start without being asked: at a time of day, or when a program comes and goes.

Whether a preset is due is a pure function of the preset and a moment in time, so none of these
tests waits for a clock - they hand one in. The behaviour worth naming is the second tick inside
the same minute: a schedule that fires twice because the loop ran twice would be a bug nobody
sees until the lights flicker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from nox.core.config.presets import PresetsConfig
from nox.core.events import Event
from nox.presets.triggers import PresetTriggers, is_due

# 2026-09-21 is a Monday.
MONDAY_2000 = datetime(2026, 9, 21, 20, 0)
TUESDAY_2000 = datetime(2026, 9, 22, 20, 0)

CONFIG = PresetsConfig.model_validate(
    {
        "items": [
            {
                "id": "evening",
                "name": "Abend",
                "triggers": {"at": "20:00"},
                "steps": [{"kind": "say", "text": "Guten Abend."}],
            },
            {
                "id": "workday",
                "name": "Arbeitstag",
                "triggers": {"at": "20:00", "days": ["mon"]},
                "steps": [{"kind": "say", "text": "Montag."}],
            },
            {
                "id": "gaming",
                "name": "Gaming",
                "triggers": {"on_process_start": "RocketLeague.exe"},
                "steps": [{"kind": "say", "text": "Viel Erfolg."}],
            },
            {
                "id": "after_gaming",
                "name": "Nach dem Spiel",
                "triggers": {"on_process_end": "RocketLeague.exe"},
                "steps": [{"kind": "say", "text": "Feierabend."}],
            },
        ]
    }
)


class FakeBus:
    def subscribe(self, _name: str, _handler: Any) -> Any:
        return lambda: None


class FakeRunner:
    def __init__(self) -> None:
        self.activated: list[tuple[str, str]] = []

    async def activate(self, preset_id: str, *, trigger: str = "manual", **_: Any) -> None:
        self.activated.append((preset_id, trigger))


def triggers(now: datetime, config: PresetsConfig = CONFIG) -> tuple[PresetTriggers, FakeRunner]:
    runner = FakeRunner()
    scheduler = PresetTriggers(
        runner=runner,  # type: ignore[arg-type]
        bus=FakeBus(),
        settings=lambda: config,
        clock=lambda: now,
    )
    return scheduler, runner


def preset(preset_id: str) -> Any:
    return next(item for item in CONFIG.items if item.id == preset_id)


def test_a_preset_is_due_in_its_minute() -> None:
    assert is_due(preset("evening"), MONDAY_2000)


def test_a_preset_is_not_due_a_minute_later() -> None:
    assert not is_due(preset("evening"), MONDAY_2000.replace(minute=1))


def test_a_preset_without_a_time_is_never_due() -> None:
    assert not is_due(preset("gaming"), MONDAY_2000)


def test_weekdays_are_honoured() -> None:
    assert is_due(preset("workday"), MONDAY_2000)
    assert not is_due(preset("workday"), TUESDAY_2000)


@pytest.mark.asyncio
async def test_a_tick_runs_everything_due_in_that_minute() -> None:
    scheduler, runner = triggers(MONDAY_2000)

    ran = await scheduler.tick()

    assert sorted(ran) == ["evening", "workday"]
    assert all(trigger == "schedule" for _, trigger in runner.activated)


@pytest.mark.asyncio
async def test_a_second_tick_in_the_same_minute_changes_nothing() -> None:
    scheduler, runner = triggers(MONDAY_2000)

    await scheduler.tick()
    again = await scheduler.tick()

    assert again == []
    assert len(runner.activated) == 2


@pytest.mark.asyncio
async def test_a_started_process_activates_its_preset() -> None:
    scheduler, runner = triggers(MONDAY_2000)
    event = Event(name="sensor.process_started", payload={"process": "rocketleague.exe", "pid": 1})

    await scheduler._on_process_started(event)

    assert runner.activated == [("gaming", "process")]


@pytest.mark.asyncio
async def test_an_ended_process_activates_its_own_preset() -> None:
    scheduler, runner = triggers(MONDAY_2000)
    event = Event(name="sensor.process_ended", payload={"process": "RocketLeague.exe", "pid": 1})

    await scheduler._on_process_ended(event)

    assert runner.activated == [("after_gaming", "process")]


@pytest.mark.asyncio
async def test_another_program_starting_changes_nothing() -> None:
    scheduler, runner = triggers(MONDAY_2000)
    event = Event(name="sensor.process_started", payload={"process": "notepad.exe", "pid": 1})

    await scheduler._on_process_started(event)

    assert runner.activated == []


@pytest.mark.asyncio
async def test_nothing_runs_while_presets_are_switched_off() -> None:
    disabled = PresetsConfig.model_validate({**CONFIG.model_dump(), "enabled": False})
    scheduler, runner = triggers(MONDAY_2000, disabled)

    assert await scheduler.tick() == []
    assert runner.activated == []
