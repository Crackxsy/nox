"""A tool's preflight can only make the permission decision stricter, and names the real target.

The home plugin uses it to look inside a scene before activating it: a scene that sets a lock is
refused, a switch that looks like a garage relay needs a confirmation that names it. These tests
pin the executor half of that contract with plain fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from nox.core.events import E
from nox.security.audit import SqliteAuditLog
from nox.security.model import Decision, Profile, ProfileRule, Risk
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.profiles import InMemoryProfileProvider
from nox.tools.executor import ERR_PERMISSION_DENIED, ToolExecutor
from nox.tools.registry import PreflightVerdict, ToolRegistry, ToolSpec
from tests.unit.fakes import FakeBus, MutableClock
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy


class SceneInput(BaseModel):
    entity_id: str


def _spec(
    verdict: PreflightVerdict | Exception | None, calls: list[dict[str, Any]], *, delay: float = 0
) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments)
        return {"ok": True}

    async def preflight(_payload: dict[str, Any]) -> PreflightVerdict:
        if delay:
            await asyncio.sleep(delay)
        if isinstance(verdict, Exception):
            raise verdict
        assert verdict is not None
        return verdict

    return ToolSpec(
        name="home.scene",
        description="",
        input_model=SceneInput,
        risk=Risk.MEDIUM,
        handler=handler,
        preflight=None if verdict is None else preflight,
    )


def _executor(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> tuple[ToolExecutor, DefaultPermissionEngine]:
    profile = Profile(
        id="test",
        rules=[ProfileRule(id="test.scene", tool="home", action="scene", decision="allow")],
        cloud_allowed=True,
    )
    engine = DefaultPermissionEngine(
        profiles=InMemoryProfileProvider([profile]),
        privacy=FakePrivacy(),
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        clock=clock,
        initial_profile="test",
        confirm_timeout_s=0.5,
    )
    return ToolExecutor(registry, engine, audit, bus, FakeKillSwitch(bus), timeout_s=2.0), engine


async def test_a_preflight_deny_refuses_the_call_before_the_engine_is_asked(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> None:
    calls: list[dict[str, Any]] = []
    verdict = PreflightVerdict(
        decision=Decision.DENY, targets=["lock.front_door"], reason="scene sets a lock"
    )
    registry.register(_spec(verdict, calls))
    executor, _ = _executor(registry, audit, bus, clock)

    result = await executor.call("dashboard", "home.scene", {"entity_id": "scene.x"}, mode="c")

    assert result.ok is False and result.error == ERR_PERMISSION_DENIED
    assert result.data == {"reason": "scene sets a lock", "refused": True}
    assert calls == []
    row = audit.entries()[-1]
    assert (row.decision, row.result, row.target) == ("deny", "refused", "lock.front_door")


async def test_a_preflight_confirm_turns_an_allow_into_a_confirmation_naming_the_target(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> None:
    calls: list[dict[str, Any]] = []
    verdict = PreflightVerdict(decision=Decision.CONFIRM, targets=["switch.garage_relay"])
    registry.register(_spec(verdict, calls))
    executor, engine = _executor(registry, audit, bus, clock)

    async def approve() -> None:
        event = await bus.wait_for(E.SECURITY_PERMISSION_REQUESTED, timeout=2.0)
        assert event.payload["target"] == "switch.garage_relay"
        engine.reply(event.payload["request_id"], Decision.ALLOW)

    approver = asyncio.create_task(approve())
    result = await executor.call("dashboard", "home.scene", {"entity_id": "scene.x"}, mode="c")
    await approver

    assert result.ok is True
    assert calls == [{"entity_id": "scene.x"}]


async def test_a_preflight_confirm_without_an_answer_never_runs_the_tool(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> None:
    calls: list[dict[str, Any]] = []
    registry.register(_spec(PreflightVerdict(decision=Decision.CONFIRM), calls))
    executor, _ = _executor(registry, audit, bus, clock)

    result = await executor.call("dashboard", "home.scene", {"entity_id": "scene.x"}, mode="c")

    assert result.ok is False and result.error == ERR_PERMISSION_DENIED
    assert calls == []


async def test_a_failing_preflight_refuses_the_call(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> None:
    calls: list[dict[str, Any]] = []
    registry.register(_spec(ConnectionError("plugin gone"), calls))
    executor, _ = _executor(registry, audit, bus, clock)

    result = await executor.call("dashboard", "home.scene", {"entity_id": "scene.x"}, mode="c")

    assert result.ok is False and result.error == ERR_PERMISSION_DENIED
    assert result.data is not None and result.data["refused"] is True
    assert calls == []


async def test_a_preflight_allow_leaves_the_engine_decision_alone(
    registry: ToolRegistry, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> None:
    calls: list[dict[str, Any]] = []
    registry.register(_spec(PreflightVerdict(targets=["light.a", "light.b"]), calls))
    executor, _ = _executor(registry, audit, bus, clock)

    result = await executor.call("dashboard", "home.scene", {"entity_id": "scene.x"}, mode="c")

    assert result.ok is True
    assert calls == [{"entity_id": "scene.x"}]
    assert audit.entries()[-1].target == "light.a, light.b"
