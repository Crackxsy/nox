"""`sensors.status.read`: read-only latest-sample snapshot, read risk, local, no side effects."""

from __future__ import annotations

from nox.security.model import Risk
from nox.sensors.history import SensorHistoryStore
from nox.sensors.tools import make_sensors_status_read_tool


async def test_returns_latest_sample_per_sensor_or_all() -> None:
    history = SensorHistoryStore(maxlen=10)
    history.record("resources", {"cpu": 5.0})
    history.record("resources", {"cpu": 6.0})
    tool = make_sensors_status_read_tool(history)

    assert tool.risk is Risk.READ
    assert tool.local is True
    assert tool.side_effects is False

    everything = await tool.handler({})
    assert everything == {"sensors": {"resources": {"cpu": 6.0}}}

    one = await tool.handler({"sensor": "resources"})
    assert one == {"sensors": {"resources": {"cpu": 6.0}}}

    missing = await tool.handler({"sensor": "idle"})
    assert missing == {"sensors": {"idle": None}}
