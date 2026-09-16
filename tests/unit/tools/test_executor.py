"""nox.tools.executor: allow/confirm/deny, hard prohibition, PRIVATE + cloud tool, kill switch
cancellation, audit entries never carry tool input/output."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.model import Decision, Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.tools.executor import (
    ERR_CANCELLED,
    ERR_INVALID_INPUT,
    ERR_PERMISSION_DENIED,
    ERR_UNKNOWN_TOOL,
    ToolExecutor,
    split_tool_action,
)
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.fakes import FakeBus
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy


class EmptyInput(BaseModel):
    pass


class TextInput(BaseModel):
    text: str


def echo_spec(name: str = "echo.say", *, risk: Risk = Risk.LOW, local: bool = True) -> ToolSpec:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"text": arguments.get("text", "")}

    return ToolSpec(
        name=name, description="", input_model=TextInput, risk=risk, handler=handler, local=local
    )


def slow_spec(name: str, *, seconds: float, risk: Risk = Risk.READ) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return {"done": True}

    return ToolSpec(name=name, description="", input_model=EmptyInput, risk=risk, handler=handler)


def make_executor(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
    *,
    timeout_s: float = 5.0,
) -> ToolExecutor:
    return ToolExecutor(registry, engine, audit, bus, killswitch, timeout_s=timeout_s)


def test_split_tool_action() -> None:
    assert split_tool_action("obs.scene.switch") == ("obs", "scene.switch")
    assert split_tool_action("time.now") == ("time", "now")
    assert split_tool_action("bare") == ("bare", "")


# ---- unknown / invalid input ----------------------------------------------------------------


async def test_unknown_tool_refused_and_audited(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "no.such.tool", {}, mode="companion")
    assert result.ok is False
    assert result.error == ERR_UNKNOWN_TOOL
    assert result.decision == Decision.DENY.value
    entries = audit.entries()
    assert len(entries) == 1
    assert entries[0].result == "refused"
    assert entries[0].tool == "no"
    assert entries[0].action == "such.tool"


async def test_invalid_input_refused_before_permission_check(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec())
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "echo.say", {"wrong_field": 1}, mode="companion")
    assert result.ok is False
    assert result.error == ERR_INVALID_INPUT
    assert audit.entries()[0].result == "validation_failed"


# ---- allow / deny / confirm -------------------------------------------------------------------


async def test_allow_path_runs_handler(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec())
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "echo.say", {"text": "hi"}, mode="companion")
    assert result.ok is True
    assert result.data == {"text": "hi"}
    assert result.decision == Decision.ALLOW.value
    assert audit.entries()[0].result == "ok"


async def test_explicit_profile_deny(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec("secret.reveal"))
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "secret.reveal", {"text": "x"}, mode="companion")
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED
    assert result.decision == Decision.DENY.value
    assert audit.entries()[0].result == "denied"


async def test_hard_prohibition_denied_regardless_of_profile(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec("stream.stop", risk=Risk.LOW))
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "stream.stop", {"text": "x"}, mode="stream")
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED
    assert result.decision == Decision.DENY.value


async def test_confirm_then_allow(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec("scene.switch", risk=Risk.MEDIUM))
    executor = make_executor(registry, engine, audit, bus, killswitch)

    async def confirm_when_requested() -> None:
        event = await bus.wait_for(E.SECURITY_PERMISSION_REQUESTED)
        engine.reply(event.payload["request_id"], Decision.ALLOW)

    result, _ = await asyncio.gather(
        executor.call("nox.chat", "scene.switch", {"text": "Live"}, mode="stream"),
        confirm_when_requested(),
    )
    assert result.ok is True
    assert result.decision == Decision.ALLOW.value
    assert audit.entries()[-1].result == "ok"


async def test_confirm_timeout_denies(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec("scene.switch", risk=Risk.MEDIUM))
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "scene.switch", {"text": "Live"}, mode="stream")
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED
    assert result.decision == Decision.DENY.value


# ---- privacy mode -----------------------------------------------------------------------------


async def test_private_mode_denies_cloud_tool(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
    privacy: FakePrivacy,
) -> None:
    registry.register(echo_spec("web.fetch", risk=Risk.LOW, local=False))
    privacy.mode = PrivacyMode.PRIVATE
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "web.fetch", {"text": "x"}, mode="companion")
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED
    assert result.decision == Decision.DENY.value


async def test_private_mode_still_allows_local_read_tool(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
    privacy: FakePrivacy,
) -> None:
    registry.register(echo_spec("echo.say", risk=Risk.READ, local=True))
    privacy.mode = PrivacyMode.PRIVATE
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "echo.say", {"text": "x"}, mode="companion")
    assert result.ok is True


# ---- kill switch -------------------------------------------------------------------------------


async def test_kill_switch_cancels_running_tool(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(slow_spec("slow.read", seconds=10))
    executor = make_executor(registry, engine, audit, bus, killswitch, timeout_s=5)

    async def trigger_kill() -> None:
        await asyncio.sleep(0.01)
        await killswitch.trigger(by="test", reason="stop")

    result, _ = await asyncio.gather(
        executor.call("nox.chat", "slow.read", {}, mode="companion"),
        trigger_kill(),
    )
    assert result.ok is False
    assert result.error == ERR_CANCELLED
    assert audit.entries()[-1].result == "aborted"


async def test_timeout_without_kill_switch(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(slow_spec("slow.read", seconds=1.0))
    executor = make_executor(registry, engine, audit, bus, killswitch, timeout_s=0.05)
    result = await executor.call("nox.chat", "slow.read", {}, mode="companion")
    assert result.ok is False
    assert result.error == "tool.timeout"


async def test_engaged_kill_switch_blocks_before_run(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec("echo.say", risk=Risk.READ))
    await killswitch.trigger(by="test", reason="pre-engaged")
    executor = make_executor(registry, engine, audit, bus, killswitch)
    result = await executor.call("nox.chat", "echo.say", {"text": "hi"}, mode="companion")
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED


# ---- audit never carries private content -------------------------------------------------------


async def test_audit_details_never_carry_arguments_or_results(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> None:
    registry.register(echo_spec())
    executor = make_executor(registry, engine, audit, bus, killswitch)
    secret_text = "super-secret-private-content"
    await executor.call("nox.chat", "echo.say", {"text": secret_text}, mode="companion")
    seq = audit.entries()[-1].seq
    details = audit.details(seq)
    assert secret_text not in " ".join(details.values())
    assert set(details) == {"duration_ms"}
