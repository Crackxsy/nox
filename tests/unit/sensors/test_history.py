"""In-memory sensor ring buffer: bounded per-sensor, FIFO eviction."""

from __future__ import annotations

from nox.sensors.history import SensorHistoryStore


def test_ring_buffer_is_bounded_per_sensor() -> None:
    store = SensorHistoryStore(maxlen=3)
    for i in range(5):
        store.record("resources", {"cpu": float(i)})

    recent = store.recent("resources")

    assert len(recent) == 3
    assert [r["cpu"] for r in recent] == [2.0, 3.0, 4.0]
    assert store.latest("resources") == {"cpu": 4.0}


def test_unknown_sensor_reads_as_empty() -> None:
    store = SensorHistoryStore(maxlen=3)

    assert store.latest("idle") is None
    assert store.recent("idle") == []
    assert store.snapshot() == {}
