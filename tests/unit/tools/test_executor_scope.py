"""A tool with a per-call `CallScope` (obs.request) through the real permission engine and the
shipped `stream` profile: reads run, changes are confirmed and remembered per request type,
destructive changes are confirmed every time, hard prohibitions and blocked types never run, and
the privacy scene is out of reach. Each rule here fails if its guard is removed."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from nox.core.events import E
from nox.paths import PROFILES_DIR
from nox.security.audit import SqliteAuditLog
from nox.security.model import Decision, PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.profiles import YamlProfileProvider
from nox.stream.obs_requests import scope_for_tool
from nox.tools.executor import ERR_INVALID_INPUT, ERR_PERMISSION_DENIED, ToolExecutor, ToolResult
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.fakes import FakeBus, MutableClock
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy

PRIVACY_SCENE = "Technical Problems"


class RequestInput(BaseModel):
    request_type: str
    request_data: dict[str, Any] = Field(default_factory=dict)


@pytest.fixture
def sent() -> list[str]:
    return []


@pytest.fixture
def stream_engine(
    privacy: FakePrivacy, audit: SqliteAuditLog, bus: FakeBus, clock: MutableClock
) -> DefaultPermissionEngine:
    return DefaultPermissionEngine(
        profiles=YamlProfileProvider(PROFILES_DIR),
        privacy=privacy,
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        clock=clock,
        initial_profile="stream",
        confirm_timeout_s=0.2,
    )


@pytest.fixture
def executor(
    sent: list[str],
    stream_engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> ToolExecutor:
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        sent.append(str(arguments["request_type"]))
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="obs.request",
            description="",
            input_model=RequestInput,
            risk=Risk.HIGH,
            handler=handler,
            scope=scope_for_tool({"privacy_scene": PRIVACY_SCENE}),
        )
    )
    return ToolExecutor(registry, stream_engine, audit, bus, killswitch, timeout_s=5.0)


async def call(executor: ToolExecutor, request_type: str, **data: Any) -> ToolResult:
    return await executor.call(
        "nox.chat",
        "obs.request",
        {"request_type": request_type, "request_data": data},
        mode="stream",
    )


async def call_answering(
    executor: ToolExecutor,
    engine: DefaultPermissionEngine,
    bus: FakeBus,
    request_type: str,
    *,
    allow: bool = True,
    remember: bool = False,
    **data: Any,
) -> tuple[ToolResult, dict[str, Any]]:
    """Run one call and answer the confirmation it raises; returns the result and the request."""

    async def answer() -> dict[str, Any]:
        event = await bus.wait_for(E.SECURITY_PERMISSION_REQUESTED, timeout=2.0)
        decision = Decision.ALLOW if allow else Decision.DENY
        engine.reply(event.payload["request_id"], decision, remember=remember)
        return dict(event.payload)

    result, asked = await asyncio.gather(call(executor, request_type, **data), answer())
    return result, asked


async def test_read_runs_without_asking(
    executor: ToolExecutor, sent: list[str], bus: FakeBus
) -> None:
    result = await call(executor, "GetSceneList")
    assert result.ok is True
    assert sent == ["GetSceneList"]
    assert not [e for e in bus.published if e.name == E.SECURITY_PERMISSION_REQUESTED]


async def test_change_without_confirmation_never_runs(
    executor: ToolExecutor, sent: list[str]
) -> None:
    # Nobody answers: the 0.2 s confirm timeout denies.
    result = await call(executor, "CreateScene", sceneName="Just Chatting")
    assert result.ok is False and result.error == ERR_PERMISSION_DENIED
    assert sent == []


async def test_confirmed_change_runs_and_names_its_type(
    executor: ToolExecutor,
    stream_engine: DefaultPermissionEngine,
    bus: FakeBus,
    sent: list[str],
) -> None:
    result, asked = await call_answering(
        executor, stream_engine, bus, "CreateScene", sceneName="Just Chatting"
    )
    assert result.ok is True and sent == ["CreateScene"]
    assert asked["action"] == "request.CreateScene" and asked["risk"] == "medium"


async def test_remember_covers_only_that_request_type(
    executor: ToolExecutor,
    stream_engine: DefaultPermissionEngine,
    bus: FakeBus,
    sent: list[str],
) -> None:
    await call_answering(executor, stream_engine, bus, "CreateScene", remember=True, sceneName="A")
    again = await call(executor, "CreateScene", sceneName="B")
    assert again.ok is True  # remembered: no question this time
    other = await call(executor, "SetInputMute", inputName="Mic", inputMuted=True)
    assert other.ok is False and other.error == ERR_PERMISSION_DENIED  # asked, nobody answered
    assert sent == ["CreateScene", "CreateScene"]


async def test_remove_is_asked_every_time_even_when_remembered(
    executor: ToolExecutor,
    stream_engine: DefaultPermissionEngine,
    bus: FakeBus,
    sent: list[str],
) -> None:
    result, asked = await call_answering(
        executor, stream_engine, bus, "RemoveScene", remember=True, sceneName="Old"
    )
    assert result.ok is True and asked["risk"] == "high" and asked["action"] == "scene.delete"
    second = await call(executor, "RemoveScene", sceneName="Older")
    assert second.ok is False and second.error == ERR_PERMISSION_DENIED
    assert sent == ["RemoveScene"]


@pytest.mark.parametrize("request_type", ["StartStream", "StartOutput", "ToggleRecordPause"])
async def test_going_live_is_never_remembered_by_any_route(
    executor: ToolExecutor,
    stream_engine: DefaultPermissionEngine,
    bus: FakeBus,
    sent: list[str],
    request_type: str,
) -> None:
    _, asked = await call_answering(
        executor, stream_engine, bus, request_type, remember=True, outputName="adv_stream"
    )
    assert asked["risk"] == "high"
    second = await call(executor, request_type, outputName="adv_stream")
    assert second.ok is False
    assert sent == [request_type]


async def test_reads_that_may_return_secrets_are_asked(
    executor: ToolExecutor, sent: list[str]
) -> None:
    result = await call(executor, "GetProfileParameter", parameterCategory="x", parameterName="y")
    assert result.ok is False and sent == []  # asked, nobody answered


async def test_confirmation_names_what_is_changed(
    executor: ToolExecutor, stream_engine: DefaultPermissionEngine, bus: FakeBus
) -> None:
    _, asked = await call_answering(
        executor, stream_engine, bus, "CreateScene", sceneName="Just Chatting"
    )
    assert asked["target"] == "CreateScene sceneName=Just Chatting"


@pytest.mark.parametrize("request_type", ["StopStream", "ToggleStream", "StopOutput"])
async def test_stopping_the_stream_is_denied_without_asking(
    executor: ToolExecutor,
    sent: list[str],
    bus: FakeBus,
    audit: SqliteAuditLog,
    stream_engine: DefaultPermissionEngine,
    request_type: str,
) -> None:
    result = await call(executor, request_type, outputName="adv_stream")
    assert result.ok is False and result.error == ERR_PERMISSION_DENIED
    assert sent == []
    assert not [e for e in bus.published if e.name == E.SECURITY_PERMISSION_REQUESTED]
    assert [(e.action, e.decision) for e in audit.entries() if e.tool == "obs"][0] == (
        "stream.stop",
        "deny",
    )
    ruling = stream_engine.preview(
        PermissionRequest(
            agent="nox.chat", tool="obs", action="stream.stop", mode="stream", risk=Risk.HIGH
        )
    )
    assert ruling.rule_id == "hard.stream.stop"


async def test_stream_key_is_never_read(executor: ToolExecutor, sent: list[str]) -> None:
    result = await call(executor, "GetStreamServiceSettings")
    assert result.ok is False and sent == []


@pytest.mark.parametrize(
    "request_type", ["SetStreamServiceSettings", "TriggerHotkeyByName", "CallVendorRequest"]
)
async def test_blocked_types_are_denied_without_asking(
    executor: ToolExecutor, sent: list[str], bus: FakeBus, request_type: str
) -> None:
    result = await call(executor, request_type)
    assert result.ok is False and sent == []
    assert not [e for e in bus.published if e.name == E.SECURITY_PERMISSION_REQUESTED]


async def test_privacy_scene_cannot_be_removed(
    executor: ToolExecutor, sent: list[str], bus: FakeBus
) -> None:
    result = await call(executor, "RemoveScene", sceneName=PRIVACY_SCENE)
    assert result.ok is False and sent == []
    assert not [e for e in bus.published if e.name == E.SECURITY_PERMISSION_REQUESTED]


async def test_panic_blocks_changes_but_not_reads(
    executor: ToolExecutor, sent: list[str], privacy: FakePrivacy
) -> None:
    privacy.panic = True
    blocked = await call(executor, "SetCurrentProgramScene", sceneName="Live")
    assert blocked.ok is False
    read = await call(executor, "GetSceneList")
    assert read.ok is True
    assert sent == ["GetSceneList"]


async def test_malformed_type_is_invalid_input(executor: ToolExecutor, sent: list[str]) -> None:
    result = await call(executor, "getSceneList")
    assert result.ok is False and result.error == ERR_INVALID_INPUT and sent == []
