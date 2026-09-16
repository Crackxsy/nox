"""`sensors.status.read` tool (Tool Model `system_info` surface, ST-20-01..07): a read-only
snapshot of the latest sample from each running sensor. `risk=READ`, `local=True`, no side
effects - stays available in PRIVATE/OFFLINE and safe mode like `state.read`/`health.read`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nox.security.model import Risk
from nox.sensors.history import SensorHistoryStore
from nox.tools.registry import ToolSpec


class SensorsStatusReadInput(BaseModel):
    sensor: str = ""  # empty = every sensor's latest sample


def make_sensors_status_read_tool(history: SensorHistoryStore) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        sensor = str(arguments.get("sensor") or "")
        if sensor:
            return {"sensors": {sensor: history.latest(sensor)}}
        return {"sensors": history.snapshot()}

    return ToolSpec(
        name="sensors.status.read",
        description="Latest sample from one PC-awareness sensor (or all of them): rates/levels/"
        "presence only, never window content (FR-14.4/NFR-8).",
        input_model=SensorsStatusReadInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("sensor") or ""),
    )
