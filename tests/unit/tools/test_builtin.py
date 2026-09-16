"""nox.tools.builtin: state.read / health.read / time.now, wired through the real executor."""

from __future__ import annotations

from nox.core.events import HealthChanged, HealthStatus
from nox.security.audit import SqliteAuditLog
from nox.security.permissions import DefaultPermissionEngine
from nox.tools.builtin import register_v01_tools
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus, FakeState
from tests.unit.tools.conftest import FakeKillSwitch


class FakeHealth:
    def __init__(self) -> None:
        self._current = {
            "ollama": HealthChanged(component="ollama", status=HealthStatus.AVAILABLE, reason="")
        }

    def current(self) -> dict[str, HealthChanged]:
        return dict(self._current)


async def test_time_now_is_always_registered_and_allowed(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    register_v01_tools(registry)
    assert registry.get("time.now") is not None
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "time.now", {}, mode="companion")
    assert result.ok is True
    assert result.data is not None and "utc" in result.data


async def test_state_read(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    state = FakeState()
    register_v01_tools(registry, state=state)
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call(
        "nox.chat", "state.read", {"path": "assistant.mode"}, mode="companion"
    )
    assert result.ok is True
    assert result.data == {"path": "assistant.mode", "value": "companion"}


async def test_state_read_whole_tree(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    state = FakeState()
    register_v01_tools(registry, state=state)
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "state.read", {}, mode="companion")
    assert result.ok is True
    assert result.data is not None
    assert result.data["value"]["assistant"]["mode"] == "companion"


async def test_state_read_unknown_path_fails(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    state = FakeState()
    register_v01_tools(registry, state=state)
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call(
        "nox.chat", "state.read", {"path": "no.such.path"}, mode="companion"
    )
    assert result.ok is False
    assert result.error == "tool.error"


async def test_health_read_one_component(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    health = FakeHealth()
    register_v01_tools(registry, health=health)
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call(
        "nox.chat", "health.read", {"component": "ollama"}, mode="companion"
    )
    assert result.ok is True
    assert result.data == {
        "components": {"ollama": {"component": "ollama", "status": "available", "reason": ""}}
    }


async def test_health_read_all(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    health = FakeHealth()
    register_v01_tools(registry, health=health)
    executor = ToolExecutor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "health.read", {}, mode="companion")
    assert result.ok is True
    assert result.data is not None and "ollama" in result.data["components"]
