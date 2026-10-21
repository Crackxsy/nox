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
