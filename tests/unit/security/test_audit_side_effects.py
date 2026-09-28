"""An action with a side effect that cannot be audited does not happen.

Audit writes used to be best effort everywhere: `QueuedAuditLog` logged a failed insert and moved
on, so on a full disk or a broken database every tool call and every outbound request went ahead
with no record. Now the entry that authorises a side effect is written first and awaited; if it
cannot be written the action is refused with `audit.unavailable`. Records of decisions and
outcomes stay best effort - refusing to *report* a denial helps nobody.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from nox.core.state import PrivacyMode
from nox.security.audit import SqliteAuditLog
from nox.security.audit_sink import AuditUnavailableError, QueuedAuditLog, append_durably
from nox.security.egress import AUDIT_UNAVAILABLE_RULE, EgressDenied, EgressGuard
from nox.security.model import Decision, PermissionResult, Profile, Risk
from nox.tools.executor import ERR_AUDIT_UNAVAILABLE, ToolExecutor
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.fakes import FakeBus
from tests.unit.tools.conftest import FakeKillSwitch


class BrokenAudit:
    """A log that cannot write: a full disk, a locked or closed database."""

    def __init__(self) -> None:
        self.attempts = 0

    def append(self, **_kwargs: Any) -> int:
        self.attempts += 1
        raise sqlite3.OperationalError("database or disk is full")

    def verify_chain(self) -> bool:
        return True


class AllowAll:
    """A permission engine that allows everything: these tests are about the audit step."""

    def check(self, request: Any) -> Any:
        return PermissionResult(decision=Decision.ALLOW, rule_id="test.allow", reason="")

    def request_confirmation(self, request: Any) -> str:
        raise AssertionError("no confirmation in these tests")

    async def await_confirmation(self, grant_id: str, *, timeout: float | None = None) -> Any:
        raise AssertionError("no confirmation in these tests")


class Empty(BaseModel):
    pass


def _tool(name: str, calls: list[str], *, side_effects: bool) -> ToolSpec:
    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {"done": True}

    return ToolSpec(
        name=name,
        description="",
        input_model=Empty,
        risk=Risk.LOW if side_effects else Risk.READ,
        handler=handler,
        side_effects=side_effects,
    )


# ---- the durable path -----------------------------------------------------------------------


async def test_queued_durable_append_raises_when_the_write_fails() -> None:
    queued = QueuedAuditLog(BrokenAudit())
    try:
        with pytest.raises(AuditUnavailableError):
            await queued.append_durable(
                actor="a", tool="t", action="x", target="", decision="allow", result="started"
            )
    finally:
        queued.stop()


async def test_queued_durable_append_returns_the_committed_sequence_in_order() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    store = SqliteAuditLog(conn)
    queued = QueuedAuditLog(store)
    try:
        queued.append(actor="a", tool="t", action="first", target="", decision="allow", result="ok")
        seq = await queued.append_durable(
            actor="a", tool="t", action="second", target="", decision="allow", result="ok"
        )
        assert seq == 2
        assert [e.action for e in store.entries()] == ["first", "second"]
    finally:
        queued.stop()
        conn.close()


async def test_append_durably_on_a_plain_log_turns_any_failure_into_unavailable() -> None:
    with pytest.raises(AuditUnavailableError):
        await append_durably(
            BrokenAudit(), actor="a", tool="t", action="x", target="", decision="allow", result="ok"
        )


# ---- tool calls -----------------------------------------------------------------------------


async def test_a_side_effect_tool_is_refused_when_it_cannot_be_audited() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_tool("home.light", calls, side_effects=True))
    audit = BrokenAudit()
    executor = ToolExecutor(registry, AllowAll(), audit, FakeBus(), FakeKillSwitch())

    result = await executor.call("nox.chat", "home.light", {}, mode="companion")

    assert result.ok is False
    assert result.error == ERR_AUDIT_UNAVAILABLE
    assert calls == [], "the side effect ran without an audit record"


async def test_a_read_only_tool_still_answers_when_the_outcome_cannot_be_recorded() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_tool("time.now", calls, side_effects=False))
    executor = ToolExecutor(registry, AllowAll(), BrokenAudit(), FakeBus(), FakeKillSwitch())

    result = await executor.call("nox.chat", "time.now", {}, mode="companion")

    assert result.ok is True and calls == ["time.now"]


async def test_the_side_effect_is_on_record_before_it_runs() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    audit = SqliteAuditLog(conn)
    seen_before_run: list[list[str]] = []

    async def handler(_arguments: dict[str, Any]) -> dict[str, Any]:
        seen_before_run.append([e.result for e in audit.entries()])
        return {}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="home.light", description="", input_model=Empty, risk=Risk.LOW, handler=handler
        )
    )
    executor = ToolExecutor(registry, AllowAll(), audit, FakeBus(), FakeKillSwitch())
    try:
        result = await executor.call("nox.chat", "home.light", {}, mode="companion")
        assert result.ok
        assert seen_before_run == [["started"]]
        assert [e.result for e in audit.entries()] == ["started", "ok"]
    finally:
        conn.close()


# ---- outbound requests ----------------------------------------------------------------------


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        return httpx.Response(200)


class Mode:
    def __init__(self, mode: PrivacyMode) -> None:
        self.mode = mode


def _guard(audit: Any, inner: RecordingTransport, *, safe_mode: bool = False) -> EgressGuard:
    profile = Profile(
        id="test",
        description="",
        rules=[],
        cloud_allowed=True,
        egress_allowlist=["api.example.com:443"],
    )
    return EgressGuard(
        profile=lambda: profile,
        privacy=Mode(PrivacyMode.BALANCED),
        audit=audit,
        loopback_allowlist=["127.0.0.1:11434"],
        transport_factory=lambda: inner,
        safe_mode=lambda: safe_mode,
    )


async def test_a_request_that_cannot_be_audited_never_leaves_the_machine() -> None:
    inner = RecordingTransport()
    queued = QueuedAuditLog(BrokenAudit())
    try:
        async with _guard(queued, inner).client() as client:
            with pytest.raises(EgressDenied) as denied:
                await client.get("https://api.example.com/v1/ping")
        assert denied.value.rule_id == AUDIT_UNAVAILABLE_RULE
        assert inner.requests == []
    finally:
        queued.stop()


async def test_an_audited_request_goes_out() -> None:
    inner = RecordingTransport()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    queued = QueuedAuditLog(SqliteAuditLog(conn))
    try:
        async with _guard(queued, inner).client() as client:
            assert (await client.get("https://api.example.com/v1/ping")).status_code == 200
        assert inner.requests == ["https://api.example.com/v1/ping"]
    finally:
        queued.stop()
        conn.close()


def test_safe_mode_limits_egress_to_allow_listed_loopback() -> None:
    guard = _guard(None, RecordingTransport(), safe_mode=True)
    denied = guard.check("api.example.com", 443)
    assert not denied.allowed and denied.rule_id == "safe_mode"
    assert guard.check("127.0.0.1", 11434).allowed
    assert not guard.check("127.0.0.1", 47800).allowed
