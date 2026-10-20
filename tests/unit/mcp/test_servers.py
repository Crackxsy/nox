"""`McpServerManager` against a real stdio server process (`tests/_fake_mcp_server.py`)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from nox.core.config.hands import McpConfig, McpServerConfig
from nox.core.state import PrivacyMode
from nox.mcp.client import HttpTransport, McpClient, RemoteTool
from nox.mcp.launch import ProcessLauncher, RefusingLauncher, ServerLauncher
from nox.mcp.policy import fingerprint
from nox.mcp.servers import (
    ApprovalStore,
    McpServerManager,
    ServerState,
    _Server,
    clean_schema,
)
from nox.security.model import Decision, Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.security.taint import CONVERSATION
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import wait_until
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy

FAKE = Path(__file__).resolve().parents[2] / "_fake_mcp_server.py"


class Secrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.read: list[str] = []

    def get(self, name: str) -> str | None:
        self.read.append(name)
        return self.values.get(name)


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> None:
        self.rows.append(row)


class Holder:
    """Mutable config + persistence double, like the core's `config.mcp` and `user.yaml`."""

    def __init__(self, servers: list[McpServerConfig]) -> None:
        self.config = McpConfig(servers=servers, start_timeout_s=20, call_timeout_s=20)
        self.saved: list[list[McpServerConfig]] = []

    async def persist(self, servers: list[McpServerConfig]) -> None:
        self.saved.append(servers)
        self.config = self.config.model_copy(update={"servers": servers})


def server(scenario: str = "basic", **extra: Any) -> McpServerConfig:
    return McpServerConfig(
        id="fake", command=[sys.executable, str(FAKE), scenario], network=False, **extra
    )


def manager(
    tmp_path: Path,
    registry: ToolRegistry,
    holder: Holder,
    *,
    secrets: Secrets | None = None,
    audit: Audit | None = None,
    cloud: bool = True,
    approve: bool = True,
    revoked: list[str] | None = None,
    launcher: ServerLauncher | None = None,
) -> McpServerManager:
    approvals = ApprovalStore(tmp_path / "approved.json")
    if approve:
        for cfg in holder.config.servers:
            approvals.approve(cfg.id, fingerprint(cfg))
    return McpServerManager(
        config=lambda: holder.config,
        registry=registry,
        launcher=launcher or ProcessLauncher(),
        http_client=httpx.AsyncClient,
        secrets=secrets or Secrets(),
        approvals=approvals,
        workdir=tmp_path / "servers",
        allows_cloud=lambda: cloud,
        persist=holder.persist,
        audit=audit,
        env_base=lambda: {
            k: v
            for k, v in __import__("os").environ.items()
            if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONPATH")
        },
        revoke_grants=lambda prefix: (revoked.append(prefix) if revoked is not None else None) or 0,
    )


async def test_tools_appear_namespaced_and_hostile_names_are_blocked(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    try:
        assert registry.names() == ["mcp.fake.echo", "mcp.fake.get_weather"]
        status = mgr.status()[0]
        assert status["state"] == "running"
        blocked = {t["remote_name"]: t["blocked"] for t in status["tools"] if t["blocked"]}
        assert set(blocked) == {"home_unlock", "game-input-send", "security_disable", "open_garage"}
        assert "hard prohibition" in blocked["game-input-send"]
        assert "reserved namespace" in blocked["home_unlock"]
        assert "home boundary" in blocked["open_garage"]
    finally:
        await mgr.stop()
    assert registry.names() == []


async def test_foreign_descriptions_are_cut_cleaned_and_labelled(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    try:
        spec = registry.get("mcp.fake.get_weather")
        assert spec is not None
        description = spec.description
        assert description.startswith("Tool of the external MCP server 'fake'")
        assert '[[DATA source="mcp:fake"]]' in description and description.endswith("[[/DATA]]")
        # The injected closing delimiter is defused: exactly one block, opened and closed by Nox.
        assert description.count("[[/DATA]]") == 1 and description.count("[[DATA") == 1
        assert "‮" not in description and "IGNORE ALL PREVIOUS" in description
        assert len(description) < 520
        schema = spec.input_model.model_json_schema()
        assert "x-evil" not in schema
        assert "\n" not in schema["properties"]["city"]["description"]
    finally:
        await mgr.stop()


async def test_risk_is_medium_unless_the_user_marked_the_tool_read_only(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    # The server claims readOnlyHint for get_weather; that claim alone changes nothing.
    holder = Holder([server(read_only_tools=["echo"])])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    try:
        weather = registry.get("mcp.fake.get_weather")
        echo = registry.get("mcp.fake.echo")
        assert weather is not None and weather.risk is Risk.MEDIUM
        assert echo is not None and echo.risk is Risk.LOW
    finally:
        await mgr.stop()


async def test_a_call_goes_through_the_permission_engine_and_asks_first(
    tmp_path: Path,
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: Any,
    killswitch: FakeKillSwitch,
) -> None:
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    executor = ToolExecutor(registry, engine, audit, None, killswitch)
    try:
        call = asyncio.create_task(
            executor.call(
                "companion", "mcp.fake.get_weather", {"city": "Springfield"}, mode="companion"
            )
        )
        await wait_until(lambda: bool(engine.pending()))
        engine.reply(engine.pending()[0], Decision.ALLOW, remember=True)
        result = await call
        assert result.ok and result.data is not None
        assert result.data["structured"]["temp_c"] == 21
        assert "never instructions" in result.data["note"]
        # Remembered for this one tool only: the other tool of the same server still asks.
        again = await executor.call("companion", "mcp.fake.get_weather", {"city": "B"}, mode="m")
        assert again.ok
        other = await executor.call("companion", "mcp.fake.echo", {"text": "x"}, mode="m")
        assert not other.ok and other.decision == Decision.DENY.value  # timed out unanswered
    finally:
        await mgr.stop()


async def test_the_first_start_waits_for_approval_and_a_changed_command_asks_again(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder, approve=False)
    await mgr.start()
    assert mgr.status()[0]["state"] == ServerState.NEEDS_APPROVAL.value
    assert registry.names() == []
    approved = await mgr.approve("fake", by="dashboard")
    assert approved["ok"] and registry.names()
    await mgr.stop()
    holder.config = holder.config.model_copy(update={"servers": [server("other")]})
    await mgr.start()
    assert mgr.status()[0]["state"] == ServerState.NEEDS_APPROVAL.value
    assert registry.names() == []


async def test_an_unpinned_npx_server_is_blocked_and_never_started(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    unpinned = McpServerConfig(id="npxsrv", command=["npx", "-y", "some-mcp"], version="1.0.0")
    holder = Holder([unpinned])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    row = mgr.status()[0]
    assert row["state"] == "blocked" and "exact version" in row["reason"]
    added = await mgr.add(
        McpServerConfig(id="other", command=["uvx", "srv"], version="1"), by="dashboard"
    )
    assert not added["ok"] and holder.saved == []


async def test_network_servers_are_not_used_when_privacy_forbids_the_cloud(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server().model_copy(update={"network": True})])
    holder.config.servers[0] = holder.config.servers[0]
    mgr = manager(tmp_path, registry, holder, cloud=False)
    await mgr.start()
    try:
        result = await mgr.call("fake", "echo", {"text": "hi"})
        assert not result["ok"] and "privacy mode" in result["error"]
    finally:
        await mgr.stop()


async def test_a_read_only_tool_of_a_network_server_asks_again_in_a_tainted_conversation(
    tmp_path: Path,
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: Any,
    killswitch: FakeKillSwitch,
) -> None:
    # The user marked echo read-only (LOW), but the server can reach the network: once the
    # conversation holds untrusted content, injected text could pick what it sends out.
    holder = Holder([server(read_only_tools=["echo"]).model_copy(update={"network": True})])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    executor = ToolExecutor(registry, engine, audit, None, killswitch)
    spec = registry.get("mcp.fake.echo")
    assert spec is not None and spec.network is True and spec.local is False
    try:
        token = CONVERSATION.set("clean")
        try:
            clean = await executor.call("companion", "mcp.fake.echo", {"text": "a"}, mode="m")
        finally:
            CONVERSATION.reset(token)
        assert clean.ok and not engine.pending()
        engine.mark_tainted("tainted")
        token = CONVERSATION.set("tainted")
        try:
            call = asyncio.create_task(
                executor.call("companion", "mcp.fake.echo", {"text": "b"}, mode="m")
            )
            await wait_until(lambda: bool(engine.pending()))
        finally:
            CONVERSATION.reset(token)
        engine.reply(engine.pending()[0], Decision.DENY)
        assert not (await call).ok
    finally:
        await mgr.stop()


async def test_network_server_tools_are_refused_by_the_privacy_rule_offline(
    tmp_path: Path,
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: Any,
    killswitch: FakeKillSwitch,
    privacy: FakePrivacy,
) -> None:
    holder = Holder([server(read_only_tools=["echo"]).model_copy(update={"network": True})])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    executor = ToolExecutor(registry, engine, audit, None, killswitch)
    try:
        for mode in (PrivacyMode.OFFLINE, PrivacyMode.PRIVATE):
            privacy.mode = mode
            result = await executor.call("companion", "mcp.fake.echo", {"text": "x"}, mode="m")
            assert not result.ok and result.decision == Decision.DENY.value
            assert not engine.pending()
    finally:
        await mgr.stop()


class _ListOnlyClient:
    async def list_tools(self, *, max_tools: int) -> list[RemoteTool]:
        return [RemoteTool(name="lookup")]


async def test_tools_of_a_remote_url_server_are_network_tools(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    remote = McpServerConfig(
        id="remote", url="https://mcp.example.org/mcp", read_only_tools=["lookup"]
    )
    holder = Holder([remote])
    mgr = manager(tmp_path, registry, holder)
    srv = _Server(config=remote, client=_ListOnlyClient())  # type: ignore[arg-type]
    await mgr._refresh_tools(srv)
    spec = registry.get("mcp.remote.lookup")
    assert spec is not None and spec.risk is Risk.LOW
    assert spec.network is True and spec.local is False


async def test_without_a_sandbox_a_local_server_is_blocked_and_never_started(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server()])
    refusal = "no sandbox is available here (RuntimeError: bwrap is not installed)"
    mgr = manager(tmp_path, registry, holder, launcher=RefusingLauncher(refusal))
    await mgr.start()
    try:
        row = mgr.status()[0]
        assert row["state"] == ServerState.BLOCKED.value and row["reason"] == refusal
        assert row["sandboxed"] is False and registry.names() == []
        assert not (tmp_path / "servers" / "fake").exists()  # no process, no work folder
    finally:
        await mgr.stop()


async def test_arguments_naming_a_lock_are_refused_before_the_server_sees_them(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    audit = Audit()
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder, audit=audit)
    await mgr.start()
    try:
        result = await mgr.call("fake", "echo", {"text": "please open lock.front_door"})
        assert not result["ok"] and "home boundary" in result["error"]
        assert any(row["decision"] == "deny" for row in audit.rows)
    finally:
        await mgr.stop()


async def test_server_requests_for_sampling_are_answered_with_method_not_found(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server("sampling")])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    try:
        result = await mgr.call("fake", "get_weather", {"city": "X"})
        sampled = result["structured"]["sampled"]
        assert sampled["error"]["code"] == -32601
    finally:
        await mgr.stop()


async def test_secrets_are_resolved_at_start_and_a_missing_one_fails_honestly(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    ref = "secret:nox/mcp/fake/token"
    holder = Holder([server(env={"NOX_MCP_TEST_SECRET": ref})])
    # NOX_ variables are not settable: the policy blocks this server before any secret is read.
    secrets = Secrets({"nox/mcp/fake/token": "s3cr3t"})
    mgr = manager(tmp_path, registry, holder, secrets=secrets)
    await mgr.start()
    assert mgr.status()[0]["state"] == "blocked" and secrets.read == []

    holder2 = Holder([server(env={"FAKE_TOKEN": ref})])
    missing = Secrets()
    mgr2 = manager(tmp_path, registry, holder2, secrets=missing)
    await mgr2.start()
    row = mgr2.status()[0]
    assert row["state"] == "failed" and "not set" in row["reason"]
    assert row["secrets"] == ["nox/mcp/fake/token"]


async def test_halt_stops_everything_and_nothing_restarts_until_resume(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server()])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    assert registry.names()
    await mgr.halt()
    assert registry.names() == []
    approved = await mgr.approve("fake", by="dashboard")
    assert not approved["ok"] and registry.names() == []
    await mgr.resume()
    try:
        assert registry.names()
    finally:
        await mgr.stop()


async def test_a_crashing_server_ends_failed_or_stopped_without_tools(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server("crash")])
    mgr = manager(tmp_path, registry, holder)
    await mgr.start()
    await mgr.stop()
    assert registry.names() == []
    assert mgr.status()[0]["state"] in ("failed", "stopped")


async def test_add_remove_persist_and_audit(tmp_path: Path, registry: ToolRegistry) -> None:
    audit = Audit()
    holder = Holder([])
    mgr = manager(tmp_path, registry, holder, audit=audit, approve=False)
    added = await mgr.add(server(), by="dashboard")
    assert added == {"ok": True, "state": "needs_approval"}
    assert holder.config.servers[0].id == "fake"
    await mgr.approve("fake", by="dashboard")
    assert registry.names()
    off = await mgr.set_enabled("fake", False, by="dashboard")
    assert off["state"] == "disabled" and registry.names() == []
    removed = await mgr.remove("fake", by="dashboard")
    assert removed["ok"] and holder.config.servers == []
    actions = [row["action"] for row in audit.rows]
    assert actions == ["server.add", "server.approve", "server.disable", "server.remove"]


# ---- HTTP transport ------------------------------------------------------------------------------


async def test_http_transport_speaks_json_and_sse_and_keeps_the_session() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "r"}}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                headers={"mcp-session-id": "abc"},
            )
        if "id" not in body:
            return httpx.Response(202)
        payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "t"}]}}
        events = f"event: message\ndata: {json.dumps(payload)}\n\n"
        return httpx.Response(200, text=events, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    mcp = McpClient(HttpTransport("https://r.example/mcp", client, headers={"X-A": "1"}))
    await mcp.initialize()
    tools = await mcp.list_tools()
    await mcp.close()
    assert [t.name for t in tools] == ["t"]
    assert seen[-1].headers["mcp-session-id"] == "abc"
    assert seen[-1].headers["mcp-protocol-version"] == "2025-06-18"
    assert seen[-1].headers["x-a"] == "1"


def test_clean_schema_keeps_only_known_keywords() -> None:
    schema = clean_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "string", "pattern": "x", "description": "d\n<s>"}},
            "$defs": {"x": {}},
            "required": ["a", 3],
        }
    )
    assert schema == {
        "type": "object",
        "properties": {"a": {"type": "string", "description": "d <s>"}},
        "required": ["a"],
    }


@pytest.mark.parametrize("bad", [None, [], "x"])
def test_clean_schema_of_garbage_is_an_empty_object(bad: Any) -> None:
    assert clean_schema(bad) == {}


def test_clean_schema_drops_unknown_numeric_keywords() -> None:
    assert clean_schema({"type": "object", "x-weird": 7, "minimum": 1}) == {
        "type": "object",
        "minimum": 1,
        "properties": {},
    }


# ---- remembered grants end with the server they were given for ------------------------------


async def test_remove_disable_and_a_new_approval_forget_remembered_grants(
    tmp_path: Path, registry: ToolRegistry
) -> None:
    holder = Holder([server(enabled=False)])
    revoked: list[str] = []
    mgr = manager(tmp_path, registry, holder, revoked=revoked)
    await mgr.start()
    assert (await mgr.set_enabled("fake", False, by="dashboard"))["ok"]
    assert revoked == ["session:mcp/fake."]
    await mgr.approve("fake", by="dashboard")
    assert revoked[-1] == "session:mcp/fake."
    assert len(revoked) == 2
    assert (await mgr.remove("fake", by="dashboard"))["ok"]
    assert revoked == ["session:mcp/fake."] * 3


def test_the_prefix_reaches_one_server_only(engine: DefaultPermissionEngine) -> None:
    from datetime import UTC, datetime, timedelta

    from nox.security.model import Risk, TemporaryGrant

    until = datetime.now(UTC) + timedelta(hours=1)
    for gid, scope in (("a", "session:mcp/weather.forecast"), ("b", "session:mcp/weather_x.now")):
        engine.grant_temporary(
            TemporaryGrant(
                grant_id=gid, scope=scope, max_risk=Risk.MEDIUM, expires_at=until, granted_by="user"
            )
        )
    assert engine.revoke_scope("session:mcp/weather.") == 1  # weather_x keeps its grant
    assert engine.revoke_scope("session:mcp/weather_x.") == 1
