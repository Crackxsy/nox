"""The node against a deliberately permissive core: the real node listener over real TLS, driven
by the test, which sends exactly what a compromised core would. The node must refuse on its own
authority - whatever the core decided."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.mcp.client import McpClientError
from nox.node.agent import NodeAgent
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.listener import NodeListener, NodeSession
from nox.node.manifest import NodeManifest, save_manifest
from nox.node.pairing import NodePairing
from nox.node.tools import NodeTool, default_tools
from nox.security.audit import SqliteAuditLog
from tests.unit.node.conftest import key_pem, pair_node


class FakeNotifier:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    async def show(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _never(_args: BaseModel) -> dict[str, Any]:
    raise AssertionError("a refused tool's handler ran")


def _meta(call_id: str = "c1", risk: str = "low") -> dict[str, Any]:
    return {"call_id": call_id, "risk": risk, "deadline": 4102444800.0}


async def _session(listener: NodeListener, node_id: str) -> NodeSession:
    for _ in range(250):
        session = listener.sessions.get(node_id)
        if session is not None and session.ready.is_set():
            return session
        await asyncio.sleep(0.02)
    raise AssertionError("the node never connected")


class Rig:
    def __init__(
        self,
        home: NodeHome,
        agent: NodeAgent,
        gate: NodeGate,
        notifier: FakeNotifier,
        audit: SqliteAuditLog,
        node_id: str,
        task: asyncio.Task[str],
        stop: asyncio.Event,
    ) -> None:
        self.home, self.agent, self.gate, self.notifier = home, agent, gate, notifier
        self.audit, self.node_id, self.task, self.stop = audit, node_id, task, stop


@pytest.fixture
async def rig(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> AsyncIterator[Rig]:
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(tmp_path / "node-home")
    home.ensure()
    home.key_file.write_bytes(key_pem(node.key))
    home.save_pairing(
        NodeIdentity(
            node_id=node.node_id,
            name="Laptop",
            core_host="127.0.0.1",
            core_port=47843,
            node_port=listener.port,
            ca_fingerprint=tls.fingerprint.replace(":", "").lower(),
            paired_at=datetime.now(UTC),
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    notifier = FakeNotifier()
    tools = default_tools(notifier)
    # A high-risk tool in an enabled group: it exists, but the node must still refuse it.
    tools["system.reboot"] = NodeTool("system.reboot", "system", "high", "test", _Args, _never)
    tools["shell.run"] = NodeTool("shell.run", "shell", "medium", "test", _Args, _never)
    audit = SqliteAuditLog(__import__("sqlite3").connect(":memory:", check_same_thread=False))
    gate = NodeGate(tools, NodeManifest(), audit)
    agent = NodeAgent(home, NodeKeyStore(home.key_file, None), gate, audit, ping_interval_s=3600)
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    try:
        yield Rig(home, agent, gate, notifier, audit, node.node_id, task, stop)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def _call(
    session: NodeSession, name: str, arguments: Any = None, **meta: Any
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": arguments if arguments is not None else {}}
    if meta.get("no_meta") is not True:
        params["_nox"] = _meta(**{k: v for k, v in meta.items() if k != "no_meta"})
    return await session.client.request("tools/call", params, timeout_s=5)


async def test_the_node_lists_only_what_it_offers(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    tools = await session.client.list_tools()
    # system.reboot (high) and shell.run (group off) exist on the node but are not offered.
    assert sorted(t.name for t in tools) == ["notify.show", "system.info"]


async def test_allowed_calls_run(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    info = await _call(session, "system.info", risk="read")
    assert info["structuredContent"]["cpu_count"] >= 1
    shown = await _call(session, "notify.show", {"title": "Nox", "text": "Wäsche aufhängen"})
    assert shown["structuredContent"] == {"shown": True}
    assert rig.notifier.shown == [("Nox", "Wäsche aufhängen")]


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("game.input.send", "hard_prohibition"),
        ("anticheat.bypass", "hard_prohibition"),
        ("game.memory.read", "hard_prohibition"),
        ("shell.run", "capability_disabled"),
        ("system.reboot", "risk_ceiling"),
        ("desktop.type_text", "unknown_tool"),
    ],
)
async def test_a_permissive_core_is_refused_on_the_node(
    rig: Rig, listener: NodeListener, name: str, reason: str
) -> None:
    session = await _session(listener, rig.node_id)
    answer = await _call(session, name, risk="read")  # the core even claims it is harmless
    assert answer["isError"] is True and answer["_nox"]["refused"] == reason
    denied = [e for e in rig.audit.entries(limit=100) if e.decision == "deny"]
    assert denied and denied[-1].tool == name and denied[-1].result == reason


async def test_arguments_are_checked_on_the_node(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    for arguments in ({"text": ""}, {"text": "x" * 401}, {"text": "hi", "cmd": "calc"}, ["hi"]):
        answer = await _call(session, "notify.show", arguments)
        assert answer["_nox"]["refused"] == "invalid_arguments", arguments
    assert rig.notifier.shown == []


async def test_a_call_without_call_metadata_is_refused(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    with pytest.raises(McpClientError):
        await _call(session, "system.info", no_meta=True)


async def test_the_core_can_narrow_but_never_widen(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    answer = await session.client.request("nox/policy", {"narrow": ["notify"], "widen": ["shell"]})
    assert answer["offered"] == ["system.info"]
    refused = await _call(session, "notify.show", {"text": "hi"})
    assert refused["_nox"]["refused"] == "capability_disabled"
    # "Un-narrowing" a group the device never enabled gives nothing either.
    await session.client.request("nox/policy", {"narrow": []})
    assert (await _call(session, "shell.run"))["_nox"]["refused"] == "capability_disabled"


@pytest.mark.parametrize(
    "method", ["chat.send", "security.permission.reply", "nox/blob", "initialize"]
)
async def test_methods_outside_the_core_list_are_refused(
    rig: Rig, listener: NodeListener, method: str
) -> None:
    session = await _session(listener, rig.node_id)
    with pytest.raises(McpClientError, match="not supported"):
        await session.client.request(method, {})


async def test_revocation_deletes_the_key_and_stops_the_node(
    rig: Rig, listener: NodeListener, pairing: NodePairing
) -> None:
    await _session(listener, rig.node_id)
    await pairing.revoke(rig.node_id)
    assert await asyncio.wait_for(rig.task, 5) == "revoked"
    assert not rig.home.key_file.exists() and not rig.home.cert_file.exists()
    assert rig.home.read_status()["state"] == "removed"


async def test_a_lost_connection_is_retried(rig: Rig, listener: NodeListener) -> None:
    session = await _session(listener, rig.node_id)
    await session.connection.close()
    for _ in range(250):
        again = listener.sessions.get(rig.node_id)
        if again is not None and again is not session:
            break
        await asyncio.sleep(0.02)
    assert listener.sessions[rig.node_id] is not session
    assert rig.agent.attempts >= 2


async def test_another_authority_is_never_accepted(tmp_path: Path, tls: TlsMaterial) -> None:
    home = NodeHome(tmp_path / "h")
    home.ensure()
    home.key_file.write_text("x")
    home.save_pairing(
        NodeIdentity(
            node_id="a" * 32,
            name="Laptop",
            core_host="127.0.0.1",
            core_port=1,
            node_port=1,
            ca_fingerprint="0" * 64,  # not the stored authority's
            paired_at=datetime.now(UTC),
        ),
        cert_pem="x",
        ca_pem=tls.ca_file.read_text(),
    )
    gate = NodeGate(default_tools(FakeNotifier()), NodeManifest(), None)
    agent = NodeAgent(home, NodeKeyStore(home.key_file, None), gate, None)
    assert await agent.run() == "pin_mismatch"
    assert agent.attempts == 0


async def test_an_outdated_core_protocol_offers_only_system_info(
    rig: Rig, listener: NodeListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await _session(listener, rig.node_id)
    rig.agent.compatible = False
    refused = await _call(session, "notify.show", {"text": "hi"})
    assert refused["_nox"]["refused"] == "update_needed"
    assert [t.name for t in await session.client.list_tools()] == ["system.info"]


def test_max_risk_in_node_yaml_lowers_the_ceiling(tmp_path: Path) -> None:
    path = tmp_path / "node.yaml"
    save_manifest(path, NodeManifest(max_risk="read"))
    from nox.node.manifest import load_manifest

    gate = NodeGate(default_tools(FakeNotifier()), load_manifest(path), None)
    assert [t.name for t in gate.offered()] == ["system.info"]
    assert gate.check("notify.show", {"text": "x"}).reason == "risk_ceiling"


async def test_a_result_too_large_for_a_frame_is_an_honest_error(
    rig: Rig, listener: NodeListener
) -> None:
    async def huge(_args: BaseModel) -> dict[str, Any]:
        return {"blob": "x" * (600 * 1024)}

    rig.gate._tools["system.dump"] = NodeTool("system.dump", "system", "read", "t", _Args, huge)
    session = await _session(listener, rig.node_id)
    answer = await _call(session, "system.dump", risk="read")
    assert answer["isError"] is True and "too large" in answer["content"][0]["text"]
    assert listener.sessions.get(rig.node_id) is session  # the link survived


class _ScriptedSocket:
    """Answers the hello with whatever the test wants - a core that is not the paired one."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self._answer = answer
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        import json as _json

        if self._answer is None:
            await asyncio.sleep(3600)
        answer, self._answer = self._answer, None  # type: ignore[assignment]
        return _json.dumps(answer)


@pytest.mark.parametrize("field", ["node_id", "core_id"])
async def test_a_core_that_is_not_the_paired_one_is_never_served(
    tmp_path: Path, field: str
) -> None:
    identity = NodeIdentity(
        node_id="a" * 32,
        name="Laptop",
        core_host="127.0.0.1",
        core_port=1,
        node_port=1,
        ca_fingerprint="b" * 64,
        paired_at=datetime.now(UTC),
    )
    result = {"node_id": identity.node_id, "core_id": identity.ca_fingerprint, "compatible": True}
    result[field] = "c" * (32 if field == "node_id" else 64)
    ws = _ScriptedSocket({"jsonrpc": "2.0", "id": 1, "result": result})
    gate = NodeGate(default_tools(FakeNotifier()), NodeManifest(), None)
    agent = NodeAgent(NodeHome(tmp_path / "h"), NodeKeyStore(tmp_path / "k", None), gate, None)
    outcome = await agent._session(ws, identity, asyncio.Event())  # type: ignore[arg-type]
    assert outcome == "failed" and not agent.connected.is_set()
    assert len(ws.sent) == 1  # the hello, and nothing else


async def test_arguments_that_are_not_an_object_are_refused_even_without_fields(
    rig: Rig, listener: NodeListener
) -> None:
    session = await _session(listener, rig.node_id)
    answer = await _call(session, "system.info", ["x"], risk="read")
    assert answer["_nox"]["refused"] == "invalid_arguments"
