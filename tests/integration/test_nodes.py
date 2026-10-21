"""`nox node` against the real core: network access on, real TLS, both listeners.

Pairing is a `node.pair.start` on a trusted session, then `POST /api/node/pair` with the code and
a certificate request; the certificate then opens the node listener and nothing else. Every
control has a test that goes red without it: no browser may pair a node, the code is single use,
a paired device needs the PIN, unpairing closes the node's connection and its certificate never
works again, a node stop engages the kill switch.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from nox.app import PROFILES_DIR, NoxCore
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.lan.install import LanRuntime
from nox.node.install import NodeRuntime
from nox.node.protocol import NODE_PATH, PairLink
from tests.integration.test_lan import _config as lan_config
from tests.unit.node.conftest import hello, key_pem, make_csr, make_key


def _config(tmp_path: Path) -> Any:
    config = lan_config(tmp_path)
    return config.model_copy(
        update={
            "ipc": config.ipc.model_copy(
                update={"lan": config.ipc.lan.model_copy(update={"node_port": 0})}
            )
        }
    )


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[NoxCore]:
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR)
    await core.start()
    try:
        for _ in range(300):
            if _nodes(core).running or _nodes(core).error:
                break
            await asyncio.sleep(0.05)
        assert _nodes(core).running, _nodes(core).error
        yield core
    finally:
        await core.stop()


def _lan(core: NoxCore) -> LanRuntime:
    runtime = core.extensions["lan"]
    assert isinstance(runtime, LanRuntime)
    return runtime


def _nodes(core: NoxCore) -> NodeRuntime:
    runtime = core.extensions["node"]
    assert isinstance(runtime, NodeRuntime)
    return runtime


async def _local(core: NoxCore, role: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    assert core.registry is not None
    client_id = f"{role}:t" if role != "device" else "device:" + "d" * 32
    env = Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role=role, id=client_id), payload=payload
    )  # type: ignore[arg-type]
    reply = await core.registry.dispatch(
        RequestContext(client_id=client_id, role=role, request=env), env
    )
    if reply.kind is Kind.ERROR:
        raise IpcError.from_payload(reply.payload)
    return reply.payload


async def _post_pair(
    core: NoxCore, body: dict[str, Any], *, headers: dict[str, str] | None = None
) -> httpx.Response:
    tls = _lan(core).tls
    assert tls is not None
    async with httpx.AsyncClient(verify=str(tls.ca_file)) as client:
        return await client.post(
            f"https://127.0.0.1:{_lan(core).port}/api/node/pair", json=body, headers=headers or {}
        )


async def _pair(core: NoxCore, tmp_path: Path) -> tuple[dict[str, Any], ssl.SSLContext]:
    start = await _local(core, "dashboard", "node.pair.start", {"name": "Laptop"})
    link = PairLink.parse(start["link"])
    key = make_key()
    response = await _post_pair(
        core,
        {
            "code": link.code,
            "csr": make_csr(key),
            "name": "",
            "os": "linux",
            "arch": "x86_64",
            "version": "0.7.0",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    (tmp_path / "n.crt").write_text(body["cert"])
    (tmp_path / "n.key").write_bytes(key_pem(key))
    (tmp_path / "ca.pem").write_text(body["ca_cert"])
    context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    context.load_cert_chain(str(tmp_path / "n.crt"), str(tmp_path / "n.key"))
    return body, context


async def test_a_node_pairs_with_the_link_and_connects(core: NoxCore, tmp_path: Path) -> None:
    start = await _local(core, "dashboard", "node.pair.start", {"name": "Laptop"})
    link = PairLink.parse(start["link"])
    tls = _lan(core).tls
    assert tls is not None
    # The link pins this installation's authority.
    assert link.ca_fingerprint == tls.fingerprint.replace(":", "").lower()
    assert start["qr"].startswith("data:image/svg+xml")

    body, context = await _pair(core, tmp_path)
    assert body["name"] == "Laptop" and body["node_port"] == _nodes(core).listener.port  # type: ignore[union-attr]
    ws = await connect(
        f"wss://127.0.0.1:{body['node_port']}{NODE_PATH}",
        ssl=context,
        server_hostname="localhost",
        open_timeout=5,
    )
    await ws.send(json.dumps(hello()))
    answer = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert answer["result"]["node_id"] == body["node_id"]
    listed = await _local(core, "dashboard", "node.list", {})
    assert [n["online"] for n in listed["nodes"]] == [True]
    await ws.close()


async def test_a_browser_cannot_pair_a_node(core: NoxCore) -> None:
    start = await _local(core, "dashboard", "node.pair.start", {})
    link = PairLink.parse(start["link"])
    body = {
        "code": link.code,
        "csr": make_csr(make_key()),
        "os": "linux",
        "arch": "x",
        "version": "1",
    }
    response = await _post_pair(
        core, body, headers={"Origin": f"https://127.0.0.1:{_lan(core).port}"}
    )
    assert response.status_code == 403
    # The code was not spent by the refused request.
    assert (await _post_pair(core, body)).status_code == 201


async def test_a_wrong_or_reused_code_yields_no_certificate(core: NoxCore) -> None:
    start = await _local(core, "dashboard", "node.pair.start", {})
    link = PairLink.parse(start["link"])
    body = {
        "code": link.code,
        "csr": make_csr(make_key()),
        "os": "linux",
        "arch": "x",
        "version": "1",
    }
    assert (await _post_pair(core, body)).status_code == 201
    again = await _post_pair(core, {**body, "csr": make_csr(make_key())})
    assert again.status_code == 401 and "cert" not in again.json()
    wrong = await _post_pair(core, {**body, "code": "ABCDEFGH", "csr": make_csr(make_key())})
    assert wrong.status_code == 401


async def test_a_paired_device_needs_the_pin_to_pair_a_node(core: NoxCore) -> None:
    with pytest.raises(IpcError) as refused:
        await _local(core, "device", "node.pair.start", {})
    assert refused.value.code == ERR_PERMISSION


async def test_unpairing_closes_the_node_and_its_certificate_is_dead(
    core: NoxCore, tmp_path: Path
) -> None:
    body, context = await _pair(core, tmp_path)
    url = f"wss://127.0.0.1:{body['node_port']}{NODE_PATH}"
    ws = await connect(url, ssl=context, server_hostname="localhost", open_timeout=5)
    await ws.send(json.dumps(hello()))
    await asyncio.wait_for(ws.recv(), 5)
    started = time.monotonic()
    result = await _local(core, "dashboard", "node.unpair", {"device_id": body["node_id"]})
    assert result["ok"] and result["was_online"]
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 2)
    assert time.monotonic() - started < 1.0
    with pytest.raises(InvalidStatus):
        await connect(url, ssl=context, server_hostname="localhost", open_timeout=5)


async def test_a_stop_on_the_node_engages_the_kill_switch(core: NoxCore, tmp_path: Path) -> None:
    body, context = await _pair(core, tmp_path)
    ws = await connect(
        f"wss://127.0.0.1:{body['node_port']}{NODE_PATH}",
        ssl=context,
        server_hostname="localhost",
        open_timeout=5,
    )
    await ws.send(json.dumps(hello()))
    await asyncio.wait_for(ws.recv(), 5)
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "method": "security.kill", "params": {"reason": "tray"}})
    )
    killswitch = core.security.killswitch
    for _ in range(100):
        if killswitch.is_engaged():
            break
        await asyncio.sleep(0.02)
    assert killswitch.is_engaged()
    assert killswitch.origin == "node"
    await ws.close()


async def test_a_node_enrolls_runs_and_serves_the_core(core: NoxCore, tmp_path: Path) -> None:
    """`nox node pair` + `nox node run` end to end: the node pins the authority, gets its
    certificate, connects, and the core calls its tools - which the node's gate checks."""
    from nox.node.agent import NodeAgent
    from nox.node.enroll import enroll
    from nox.node.gate import NodeGate
    from nox.node.home import NodeHome
    from nox.node.keystore import NodeKeyStore
    from nox.node.manifest import NodeManifest
    from nox.node.tools import default_tools
    from nox.security.secrets import InMemorySecretStore

    start = await _local(core, "dashboard", "node.pair.start", {"name": "Laptop"})
    printed = PairLink.parse(start["link"])
    link = PairLink.make(
        host="127.0.0.1",
        port=_lan(core).port,
        code=printed.code,
        ca_fingerprint=printed.ca_fingerprint,
    )
    home = NodeHome(tmp_path / "laptop")
    keystore = NodeKeyStore(home.key_file, InMemorySecretStore())
    identity = await enroll(link, home, keystore, name="Laptop")
    assert home.paired and b"ENCRYPTED" in home.key_file.read_bytes()

    shown: list[tuple[str, str]] = []

    class Notifier:
        async def show(self, title: str, text: str) -> None:
            shown.append((title, text))

    gate = NodeGate(default_tools(Notifier()), NodeManifest(), None)
    agent = NodeAgent(home, keystore, gate, None)
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    try:
        listener = _nodes(core).listener
        assert listener is not None
        for _ in range(250):
            session = listener.sessions.get(identity.node_id)
            if session is not None and session.ready.is_set():
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the node never connected")
        tools = await session.client.list_tools()
        assert {t.name for t in tools} == {"system.info", "notify.show"}
        meta = {"call_id": "c1", "risk": "low", "deadline": 4102444800.0}
        await session.client.request(
            "tools/call",
            {"name": "notify.show", "arguments": {"text": "Wäsche aufhängen"}, "_nox": meta},
        )
        assert shown == [("Nox", "Wäsche aufhängen")]
        refused = await session.client.request(
            "tools/call", {"name": "game.input.send", "arguments": {}, "_nox": meta}
        )
        assert refused["_nox"]["refused"] == "hard_prohibition"
        await _local(core, "dashboard", "node.unpair", {"device_id": identity.node_id})
        assert await asyncio.wait_for(task, 5) == "revoked"
        assert not home.key_file.exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_the_core_routes_tool_calls_to_the_right_device(
    core: NoxCore, tmp_path: Path
) -> None:
    """C3 end to end: `device` resolves to the paired laptop, the permission target and the audit
    name it, an unknown device is an honest error, and an offline one is never substituted."""
    from nox.node.agent import NodeAgent
    from nox.node.enroll import enroll
    from nox.node.gate import NodeGate
    from nox.node.home import NodeHome
    from nox.node.keystore import NodeKeyStore
    from nox.node.manifest import NodeManifest
    from nox.node.tools import default_tools

    start = await _local(core, "dashboard", "node.pair.start", {"name": "Laptop"})
    printed = PairLink.parse(start["link"])
    link = PairLink.make(
        host="127.0.0.1",
        port=_lan(core).port,
        code=printed.code,
        ca_fingerprint=printed.ca_fingerprint,
    )
    home = NodeHome(tmp_path / "laptop")
    keystore = NodeKeyStore(home.key_file, None)
    identity = await enroll(link, home, keystore, name="Laptop")
    shown: list[tuple[str, str]] = []

    class Notifier:
        async def show(self, title: str, text: str) -> None:
            shown.append((title, text))

    agent = NodeAgent(
        home, keystore, NodeGate(default_tools(Notifier()), NodeManifest(), None), None
    )
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    registry = _nodes(core).registry
    assert registry is not None
    try:
        for _ in range(250):
            if registry.any_online_with("notify.show"):
                break
            await asyncio.sleep(0.02)
        assert registry.any_online_with("notify.show")
        assert f"Laptop · {identity.node_id[:4]} (online)" in registry.prompt_line()
        executor = core.tool_executor
        info = await executor.call(
            "companion", "system.info", {"device": "laptop"}, mode="companion"
        )
        assert info.ok and info.data is not None
        assert info.data["device"] == f"Laptop · {identity.node_id[:4]}"
        assert info.data["result"]["cpu_count"] >= 1
        here = await executor.call("companion", "system.info", {}, mode="companion")
        assert here.ok and here.data is not None and "device" not in here.data  # the core itself
        sent = await asyncio.wait_for(
            executor.call(
                "companion", "notify.show", {"text": "Wäsche aufhängen"}, mode="companion"
            ),
            30,
        )
        assert sent.ok, sent
        assert shown == [("Nox", "Wäsche aufhängen")]
        core.security.audit.flush()
        entries = core.security.audit_store.entries(limit=500)
        targets = [e.target for e in entries if e.tool == "notify"]
        assert f"device:{identity.node_id} notify.show" in targets
        unknown = await executor.call(
            "companion", "system.info", {"device": "Toaster"}, mode="companion"
        )
        assert unknown.data is not None and unknown.data["error"] == "unknown_device"
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    for _ in range(250):
        if not registry.any_online_with("notify.show"):
            break
        await asyncio.sleep(0.02)
    offline = await core.tool_executor.call(
        "companion", "system.info", {"device": "Laptop"}, mode="companion"
    )
    assert offline.data is not None and offline.data["error"] == "device_offline"
    nobody = await core.tool_executor.call(
        "companion", "notify.show", {"text": "x"}, mode="companion"
    )
    assert nobody.data is not None and nobody.data["error"] == "no_device"
    assert shown == [("Nox", "Wäsche aufhängen")]  # nothing queued, nothing sent elsewhere
    assert core.node_probe("notify.show") is not None  # not offered while no node has it
