"""What the executor hands on and writes down: the validated input, the declared target, the
tool's own timeout, a cancelled handler on timeout and kill switch, and the target in the audit.

Each test pins one line a mutation run showed unprotected; it fails if that line is changed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from nox.security.audit import SqliteAuditLog
from nox.security.model import Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.tools.executor import ERR_CANCELLED, ERR_TIMEOUT, ToolExecutor
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.fakes import FakeBus
from tests.unit.tools.conftest import FakeKillSwitch


class CountInput(BaseModel):
    count: int
    label: str = Field(default="standard")


class PathInput(BaseModel):
    path: str


class NoInput(BaseModel):
    pass


def _executor(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
    *,
    timeout_s: float = 5.0,
) -> ToolExecutor:
    return ToolExecutor(registry, engine, audit, bus, killswitch, timeout_s=timeout_s)


async def test_the_handler_gets_the_validated_input_not_the_raw_arguments(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {}

    registry.register(
        ToolSpec(
            name="count.up", description="", input_model=CountInput, risk=Risk.READ, handler=handler
        )
    )
    executor = _executor(registry, engine, audit, bus, killswitch)

    result = await executor.call(
        "nox.chat", "count.up", {"count": "5", "smuggled": "rm -rf"}, mode="companion"
    )

    assert result.ok
    assert seen == [{"count": 5, "label": "standard"}], "coerced, defaulted, extras dropped"


async def test_the_declared_target_reaches_the_permission_check_and_the_audit(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {}

    registry.register(
        ToolSpec(
            name="file.peek",
            description="",
            input_model=PathInput,
            risk=Risk.READ,
            handler=handler,
            targets=lambda payload: str(payload["path"]),
        )
    )
    executor = _executor(registry, engine, audit, bus, killswitch)

    result = await executor.call("nox.chat", "file.peek", {"path": "notes/a.md"}, mode="companion")

    assert result.ok
    targets = [entry.target for entry in audit.entries() if entry.tool == "file"]
    assert targets, "the call must be audited"
    assert all(target == "notes/a.md" for target in targets), targets


async def test_a_tool_s_own_timeout_overrides_the_executor_default(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    """A coding session takes minutes; the executor default must not cut it off."""

    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"done": True}

    registry.register(
        ToolSpec(
            name="long.job",
            description="",
            input_model=NoInput,
            risk=Risk.READ,
            handler=handler,
            timeout_s=30.0,
        )
    )
    executor = _executor(registry, engine, audit, bus, killswitch, timeout_s=0.01)

    result = await executor.call("nox.chat", "long.job", {}, mode="companion")

    assert result.ok, result.error


def _blocking_spec(name: str, started: asyncio.Event, cancelled: asyncio.Event) -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    return ToolSpec(
        name=name,
        description="",
        input_model=NoInput,
        risk=Risk.READ,
        handler=handler,
        timeout_s=0.05,
    )


async def test_a_timed_out_handler_is_cancelled_not_left_running(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    registry.register(_blocking_spec("stuck.job", started, cancelled))
    executor = _executor(registry, engine, audit, bus, killswitch)

    result = await executor.call("nox.chat", "stuck.job", {}, mode="companion")

    assert result.error == ERR_TIMEOUT
    await asyncio.wait_for(cancelled.wait(), timeout=2)


async def test_the_kill_switch_cancels_the_running_handler(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    spec = _blocking_spec("stuck.job", started, cancelled)
    registry.register(
        ToolSpec(
            name=spec.name,
            description="",
            input_model=NoInput,
            risk=Risk.READ,
            handler=spec.handler,
            timeout_s=30.0,
        )
    )
    executor = _executor(registry, engine, audit, bus, killswitch)

    call = asyncio.create_task(executor.call("nox.chat", "stuck.job", {}, mode="companion"))
    await asyncio.wait_for(started.wait(), timeout=2)
    await killswitch.trigger(by="test")
    result = await asyncio.wait_for(call, timeout=2)

    assert result.error == ERR_CANCELLED
    await asyncio.wait_for(cancelled.wait(), timeout=2)
