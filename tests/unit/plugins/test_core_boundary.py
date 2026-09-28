"""The plugin boundary as the core enforces it, not as the plugin's own API does.

`plugin.tool.call` must reach only the tools a manifest lists under `requires.tools`, and must run
them through the `ToolExecutor` - permission engine, confirmation, audit with the plugin as the
actor. Egress decisions the plugin reports are audited, and one that names an endpoint the
manifest never declared is marked as such. The manager registers the manifest's listen/emit
boundary with the hub before a worker process exists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.ipc.errors import IpcError
from nox.plugins.egress_audit import EgressReport
from nox.plugins.manager import PluginToolCall
from nox.security.audit import SqliteAuditLog
from nox.security.model import Profile, ProfileRule, Risk
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore
from nox.security.profiles import InMemoryProfileProvider
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolSpec
from tests.unit.fakes import FakeBus, MutableClock
from tests.unit.plugins.conftest import FakeEngine, make_profile, write_manifest
from tests.unit.plugins.test_lifecycle import Harness, build
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy

START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


class QueryInput(BaseModel):
    query: str = ""


class RealEngine(DefaultPermissionEngine):
    """The real engine, plus the `active_profile()` the plugin manager reads."""

    def __init__(self, profile: Profile, **kwargs: Any) -> None:
        super().__init__(profiles=InMemoryProfileProvider([profile]), **kwargs)
        self._profile_for_manager = profile

    def active_profile(self) -> Profile:
        return self._profile_for_manager


def _loopback_engine() -> FakeEngine:
    return FakeEngine(make_profile(loopback_allowlist=["127.0.0.1:4455"]))


def _profile(*rules: ProfileRule) -> Profile:
    return Profile(id="companion", description="test", rules=list(rules), cloud_allowed=True)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


class Built:
    def __init__(self, harness: Harness, audit: SqliteAuditLog, calls: list[dict[str, Any]]):
        self.harness = harness
        self.audit = audit
        self.calls = calls


async def _built(
    plugins_dir: Path,
    conn: sqlite3.Connection,
    *,
    requires: list[str],
    profile: Profile | None = None,
) -> Built:
    write_manifest(plugins_dir, "demo", requires={"tools": requires})
    bus = FakeBus()
    clock = MutableClock(START)
    audit = SqliteAuditLog(conn, bus=bus, clock=clock)
    engine = RealEngine(
        profile or _profile(),
        privacy=FakePrivacy(),
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        clock=clock,
        initial_profile="companion",
        confirm_timeout_s=0.1,
    )
    harness = build(plugins_dir, engine=engine, audit=audit)  # type: ignore[arg-type]
    calls: list[dict[str, Any]] = []

    async def memory_search(arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(arguments)
        return {"hits": ["private note"]}

    harness.manager.tools.register(
        ToolSpec(
            name="memory.search",
            description="",
            input_model=QueryInput,
            risk=Risk.READ,
            handler=memory_search,
        )
    )
    executor = ToolExecutor(
        harness.manager.tools,  # type: ignore[arg-type]
        engine,
        audit,
        bus,
        FakeKillSwitch(bus),
        timeout_s=2.0,
    )
    harness.manager.use_executor(executor)
    await harness.manager.start()
    await harness.register()
    return Built(harness, audit, calls)


@pytest.fixture
async def stop_after() -> AsyncIterator[list[Harness]]:
    started: list[Harness] = []
    yield started
    for harness in started:
        await harness.manager.stop("test")


async def test_a_plugin_cannot_call_a_core_tool_its_manifest_does_not_require(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    built = await _built(plugins_dir, conn, requires=[])
    stop_after.append(built.harness)

    with pytest.raises(IpcError) as exc:
        await built.harness.manager._h_tool_call(
            built.harness.context(), PluginToolCall(name="memory.search", input={"query": "pw"})
        )

    assert exc.value.code == "permission.denied"
    assert built.calls == []
    denied = [e for e in built.audit.entries() if e.action == "plugin.tool.call"]
    assert len(denied) == 1
    assert denied[0].actor == "plugin:demo" and denied[0].decision == "deny"
    assert denied[0].target == "memory.search"


async def test_a_required_tool_runs_through_the_executor_and_is_audited_as_the_plugin(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    built = await _built(plugins_dir, conn, requires=["memory.search"])
    stop_after.append(built.harness)

    result = await built.harness.manager._h_tool_call(
        built.harness.context(), PluginToolCall(name="memory.search", input={"query": "x"})
    )

    assert result == {"ok": True, "result": {"hits": ["private note"]}}
    assert built.calls == [{"query": "x"}]
    memory_rows = [e for e in built.audit.entries() if e.tool == "memory"]
    assert memory_rows and all(e.actor == "plugin:demo" for e in memory_rows)
    assert any(e.result == "ok" and e.decision == "allow" for e in memory_rows)


async def test_a_required_tool_denied_by_the_profile_never_runs(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    deny_memory = ProfileRule(id="test.no_memory", tool="memory", decision="deny")
    built = await _built(
        plugins_dir, conn, requires=["memory.search"], profile=_profile(deny_memory)
    )
    stop_after.append(built.harness)

    with pytest.raises(IpcError) as exc:
        await built.harness.manager._h_tool_call(
            built.harness.context(), PluginToolCall(name="memory.search", input={"query": "x"})
        )

    assert exc.value.code == "permission.denied"
    assert built.calls == []
    assert any(e.actor == "plugin:demo" and e.result == "denied" for e in built.audit.entries())


async def test_a_required_tool_that_needs_confirmation_is_denied_without_an_answer(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    confirm = ProfileRule(id="test.confirm_memory", tool="memory", decision="confirm")
    built = await _built(plugins_dir, conn, requires=["memory.search"], profile=_profile(confirm))
    stop_after.append(built.harness)

    with pytest.raises(IpcError) as exc:
        await built.harness.manager._h_tool_call(
            built.harness.context(), PluginToolCall(name="memory.search", input={"query": "x"})
        )

    assert exc.value.code == "permission.denied"
    assert built.calls == []


async def test_without_an_executor_a_tool_call_is_refused_not_run_unchecked(
    plugins_dir: Path, stop_after: list[Harness]
) -> None:
    write_manifest(plugins_dir, "demo", requires={"tools": ["demo.ping"]})
    harness = build(plugins_dir)
    stop_after.append(harness)
    harness.hub.responses["tool.call"] = {"text": "pong"}
    await harness.manager.start()
    await harness.register()

    with pytest.raises(IpcError) as exc:
        await harness.manager._h_tool_call(
            harness.context(), PluginToolCall(name="demo.ping", input={})
        )

    assert exc.value.code == "unavailable"
    assert [r for r in harness.hub.requests if r[1] == "tool.call"] == []


async def test_egress_reports_are_audited_with_the_plugin_as_actor(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    write_manifest(plugins_dir, "demo", network={"egress": ["127.0.0.1:4455"]})
    audit = SqliteAuditLog(conn, bus=FakeBus(), clock=MutableClock(START))
    harness = build(plugins_dir, engine=_loopback_engine(), audit=audit)
    stop_after.append(harness)
    await harness.manager.start()
    await harness.register()
    ctx = harness.context()

    await harness.manager._h_egress_report(
        ctx, EgressReport(host="127.0.0.1", port=4455, scheme="ws", allowed=True)
    )
    await harness.manager._h_egress_report(
        ctx,
        EgressReport(
            host="evil.example", port=443, scheme="https", allowed=False, rule_id="x.not_declared"
        ),
    )
    await harness.manager._h_egress_report(
        ctx, EgressReport(host="evil.example", port=443, scheme="https", allowed=True)
    )

    rows = [e for e in audit.entries() if e.action == "plugin.egress"]
    assert [(r.actor, r.target, r.decision, r.result) for r in rows] == [
        ("plugin:demo", "ws://127.0.0.1:4455", "allow", "ok"),
        ("plugin:demo", "https://evil.example:443", "deny", "denied"),
        ("plugin:demo", "https://evil.example:443", "allow", "undeclared"),
    ]


async def test_egress_reports_are_rate_limited_per_plugin(
    plugins_dir: Path, conn: sqlite3.Connection, stop_after: list[Harness]
) -> None:
    write_manifest(plugins_dir, "demo", network={"egress": ["127.0.0.1:4455"]})
    audit = SqliteAuditLog(conn, bus=FakeBus(), clock=MutableClock(START))
    harness = build(plugins_dir, engine=_loopback_engine(), audit=audit, clock=lambda: 100.0)
    stop_after.append(harness)
    await harness.manager.start()
    await harness.register()
    report = EgressReport(host="127.0.0.1", port=4455, allowed=True)

    for _ in range(120):
        await harness.manager._h_egress_report(harness.context(), report)
    with pytest.raises(IpcError) as exc:
        await harness.manager._h_egress_report(harness.context(), report)

    assert exc.value.code == "rate_limited"
    assert len([e for e in audit.entries(limit=500) if e.action == "plugin.egress"]) == 120


async def test_the_listen_and_emit_boundary_reaches_the_hub_before_the_worker_spawns(
    plugins_dir: Path, stop_after: list[Harness]
) -> None:
    write_manifest(
        plugins_dir,
        "demo",
        events={"emits": ["demo.pong", "stream.started"], "listens": ["security.panic"]},
    )
    harness = build(plugins_dir)
    stop_after.append(harness)
    seen_at_spawn: list[Any] = []
    original = harness.manager._process_factory

    def factory(command: Any, env: Any) -> Any:
        seen_at_spawn.append(harness.hub.scopes.get("plugin:demo"))
        return original(command, env)

    harness.manager._process_factory = factory  # type: ignore[assignment]
    await harness.manager.start()

    scope = seen_at_spawn[0]
    assert scope is not None
    assert scope.listens == ("security.panic",)
    assert scope.emits == frozenset({"demo.pong", "stream.started"})
