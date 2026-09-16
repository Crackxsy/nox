"""The v0.1 tool catalogue that is cheap to ship now (Tool Model "Tools v0.1"): `state.read`,
`health.read`, `time.now`. All three are `read` risk, local, side-effect free, so the default
permission decision is `allow` and they stay available even in PRIVATE/OFFLINE and safe mode.

Filesystem, memory, voice, OBS and Twitch tools arrive with their own epics/stories.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from nox.core.health import HealthService
from nox.core.state import StateManager
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec


class StatePathError(KeyError):
    """Unknown dotted path passed to `state.read`."""


class StateReadInput(BaseModel):
    path: str = ""  # dotted NoxState path; empty = the whole tree


class HealthReadInput(BaseModel):
    component: str = ""  # empty = every component's current status


class TimeNowInput(BaseModel):
    pass


def make_state_read_tool(state: StateManager) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = str(arguments.get("path") or "")
        try:
            value = state.get(path)
        except KeyError as exc:
            raise StatePathError(f"unknown state path: {path}") from exc
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return {"path": path, "value": value}

    return ToolSpec(
        name="state.read",
        description="Read a value from the live NoxState tree by dotted path (empty = whole tree).",
        input_model=StateReadInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("path") or ""),
    )


def make_health_read_tool(health: HealthService) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        current = health.current()
        component = str(arguments.get("component") or "")
        if component:
            entry = current.get(component)
            return {"components": {component: entry.model_dump(mode="json")} if entry else {}}
        return {"components": {k: v.model_dump(mode="json") for k, v in current.items()}}

    return ToolSpec(
        name="health.read",
        description="Read the current health status of one component or all of them.",
        input_model=HealthReadInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
        targets=lambda payload: str(payload.get("component") or ""),
    )


def make_time_now_tool() -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {"utc": now.isoformat(), "epoch_s": now.timestamp()}

    return ToolSpec(
        name="time.now",
        description="Current UTC time.",
        input_model=TimeNowInput,
        risk=Risk.READ,
        side_effects=False,
        local=True,
        handler=handler,
    )


def register_v01_tools(
    registry: ToolRegistry,
    *,
    state: StateManager | None = None,
    health: HealthService | None = None,
) -> None:
    """Register every v0.1 tool whose dependency was supplied; `time.now` has none and is always
    registered."""
    registry.register(make_time_now_tool())
    if state is not None:
        registry.register(make_state_read_tool(state))
    if health is not None:
        registry.register(make_health_read_tool(health))
