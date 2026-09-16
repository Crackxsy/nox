"""Idle sensor: two-stage away detection (~10/20 min), activity save/restore, kill-switch pause."""

from __future__ import annotations

from nox.sensors.idle import IdleSensor
from tests.unit.fakes import FakeState

from .conftest import FakeWin32Probe

IDLE_AFTER = 600.0
AWAY_AFTER = 1200.0


def _sensor(probe: FakeWin32Probe, state: FakeState) -> IdleSensor:
    return IdleSensor(
        probe, state, poll_interval_s=1.0, idle_after_s=IDLE_AFTER, away_after_s=AWAY_AFTER
    )


async def test_two_stage_away_detection(probe: FakeWin32Probe, state: FakeState) -> None:
    sensor = _sensor(probe, state)

    probe.set_idle_seconds(0.0)
    assert await sensor.poll() == "active"
    assert state.get("user.present") is True

    probe.set_idle_seconds(IDLE_AFTER + 50)
    assert await sensor.poll() == "idle"
    assert state.get("user.present") is True  # "likely away", not away yet
    assert state.get("user.activity") == "idle"

    probe.set_idle_seconds(AWAY_AFTER + 50)
    assert await sensor.poll() == "away"
    assert state.get("user.present") is False
    assert state.get("user.activity") == "away"

    probe.set_idle_seconds(0.0)
    assert await sensor.poll() == "active"
    assert state.get("user.present") is True


async def test_resuming_restores_the_activity_that_was_live_before_going_idle(
    probe: FakeWin32Probe, state: FakeState
) -> None:
    await state.update("user.activity", "coding", reason="test-setup")
    sensor = _sensor(probe, state)

    probe.set_idle_seconds(0.0)
    await sensor.poll()  # still active: must not clobber "coding"
    assert state.get("user.activity") == "coding"

    probe.set_idle_seconds(IDLE_AFTER + 1)
    await sensor.poll()
    assert state.get("user.activity") == "idle"

    probe.set_idle_seconds(0.0)
    await sensor.poll()
    assert state.get("user.activity") == "coding"


async def test_last_input_at_tracks_idle_seconds(probe: FakeWin32Probe, state: FakeState) -> None:
    sensor = _sensor(probe, state)
    probe.set_idle_seconds(30.0)
    await sensor.poll()
    assert state.get("user.last_input_at") is not None


async def test_kill_switch_pauses_polling(probe: FakeWin32Probe, state: FakeState) -> None:
    sensor = IdleSensor(
        probe,
        state,
        idle_after_s=IDLE_AFTER,
        away_after_s=AWAY_AFTER,
        safe_mode=lambda: True,
    )
    probe.set_idle_seconds(AWAY_AFTER + 100)
    stage = await sensor.poll()
    assert stage == "active"  # untouched: sensor never ran while safe mode was engaged
    assert state.updates == []
