"""Generic game/process-lifecycle hook: sensor.process_started/ended for configured process names,
kill-switch pause."""

from __future__ import annotations

from nox.core.events import E
from nox.sensors.game import GameProcessSensor, RunningProcess
from tests.unit.fakes import FakeBus


class FakeLister:
    def __init__(self) -> None:
        self._procs: list[RunningProcess] = []

    def __call__(self) -> list[RunningProcess]:
        return self._procs

    def set(self, procs: list[RunningProcess]) -> None:
        self._procs = procs


async def test_publishes_started_then_ended_for_a_configured_process(bus: FakeBus) -> None:
    lister = FakeLister()
    sensor = GameProcessSensor(lister, bus, process_names=["RocketLeague.exe"])

    lister.set([RunningProcess("RocketLeague.exe", 4242)])
    await sensor.poll()
    started = [e for e in bus.published if e.name == E.SENSOR_PROCESS_STARTED]
    assert len(started) == 1
    assert started[0].payload == {"process": "RocketLeague.exe", "pid": 4242}

    await sensor.poll()  # still running: no duplicate started event
    assert len([e for e in bus.published if e.name == E.SENSOR_PROCESS_STARTED]) == 1

    lister.set([])
    await sensor.poll()
    ended = [e for e in bus.published if e.name == E.SENSOR_PROCESS_ENDED]
    assert len(ended) == 1
    assert ended[0].payload["process"] == "RocketLeague.exe"
    assert ended[0].payload["duration_s"] >= 0.0


async def test_ignores_unconfigured_processes(bus: FakeBus) -> None:
    lister = FakeLister()
    sensor = GameProcessSensor(lister, bus, process_names=["RocketLeague.exe"])
    lister.set([RunningProcess("notepad.exe", 1)])

    await sensor.poll()

    assert bus.published == []


async def test_kill_switch_pauses_polling(bus: FakeBus) -> None:
    lister = FakeLister()
    lister.set([RunningProcess("RocketLeague.exe", 1)])
    sensor = GameProcessSensor(
        lister, bus, process_names=["RocketLeague.exe"], safe_mode=lambda: True
    )

    await sensor.poll()

    assert bus.published == []
