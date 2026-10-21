"""The dashboard's side of a node against the real core: narrowing a capability group, giving it
back, the device as the only place that widens, and the default device per tool family.

A scripted node (raw JSON-RPC over the real mutual-TLS listener) answers `tools/list` with what
it has enabled minus what the core narrowed, exactly like `nox.node.agent` does, and records
every `nox/policy` it is sent - so a test sees what reached the device, not only what the core
believes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.client import ClientConnection, connect

from nox.app import PROFILES_DIR, NoxCore
from nox.ipc.errors import ERR_PERMISSION, ERR_VALIDATION, IpcError
from nox.node.protocol import NODE_PATH
from tests.integration.test_nodes import _config, _local, _nodes, _pair
from tests.unit.node.conftest import greet

TOOLS = {
    "system.info": ("system", "read"),
    "notify.show": ("notify", "low"),
}


@dataclass
class ScriptedNode:
    ws: ClientConnection
    narrowed: set[str] = field(default_factory=set)
    policies: list[list[str]] = field(default_factory=list)
    listings: int = 0

    def listing(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "_nox": {"group": group, "risk": risk}}
            for name, (group, risk) in TOOLS.items()
            if group not in self.narrowed
        ]

    async def serve(self) -> None:
        with contextlib.suppress(Exception):
            async for frame in self.ws:
                message = json.loads(frame)
                method, msg_id = message.get("method"), message.get("id")
                if method == "tools/list":
                    self.listings += 1
                    result: Any = {"tools": self.listing()}
                elif method == "nox/policy":
                    groups = list(message["params"]["narrow"])
                    self.policies.append(groups)
                    self.narrowed = set(groups)
                    result = {"offered": [t["name"] for t in self.listing()]}
                else:
                    continue
                if msg_id is not None:
                    await self.ws.send(
                        json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})
                    )


@pytest.fixture
async def core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[NoxCore]:
    monkeypatch.setenv("NOX_USER_CONFIG", str(tmp_path / "user.yaml"))
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


async def _connect(body: dict[str, Any], context: ssl.SSLContext) -> ScriptedNode:
    ws = await connect(
        f"wss://127.0.0.1:{body['node_port']}{NODE_PATH}",
        ssl=context,
        server_hostname="localhost",
        open_timeout=5,
    )
    await greet(ws)
    return ScriptedNode(ws)


async def _until(check: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.05)


async def _node(core: NoxCore) -> dict[str, Any]:
    listed = await _local(core, "dashboard", "node.list", {})
    assert len(listed["nodes"]) == 1
    node: dict[str, Any] = listed["nodes"][0]
    return node


def _groups(node: dict[str, Any]) -> dict[str, tuple[bool, bool]]:
    return {g["group"]: (g["enabled_on_device"], g["narrowed"]) for g in node["groups"]}


async def test_narrowing_reaches_the_node_and_the_core_routes_nothing_there(
    core: NoxCore, tmp_path: Path
) -> None:
    body, context = await _pair(core, tmp_path)
    node = await _connect(body, context)
    server = asyncio.create_task(node.serve())
    try:

        async def listed() -> bool:
            return "notify.show" in (await _node(core))["tools"]

        await _until(listed)
        first = await _node(core)
        assert _groups(first) == {"system": (True, False), "notify": (True, False)}
        assert first["os"] == "linux" and first["arch"] == "x86_64"

        # Turning a group off works from the dashboard, without a PIN on this machine.
        result = await _local(
            core,
            "dashboard",
            "node.narrow",
            {"node_id": body["node_id"], "group": "notify", "narrowed": True},
        )
        assert result["ok"]
        assert node.policies[-1] == ["notify"]
        after = await _node(core)
        assert "notify.show" not in after["tools"]
        assert _groups(after)["notify"] == (True, True)
        registry = _nodes(core).registry
        assert registry is not None
        assert not registry.any_online_with("notify.show")

        # Giving it back: the node is told, lists the group again, and it is offered again.
        await _local(
            core,
            "dashboard",
            "node.narrow",
            {"node_id": body["node_id"], "group": "notify", "narrowed": False},
        )
        assert node.policies[-1] == []
        await _until(listed)
        assert _groups(await _node(core))["notify"] == (True, False)
    finally:
        await node.ws.close()
        server.cancel()


async def test_a_group_off_on_the_device_cannot_be_switched_on_here(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    with pytest.raises(IpcError) as refused:
        await _local(
            core,
            "dashboard",
            "node.narrow",
            {"node_id": body["node_id"], "group": "shell", "narrowed": False},
        )
    assert refused.value.code == ERR_VALIDATION
    assert refused.value.details["reason"] == "enable_on_device"
    with pytest.raises(IpcError) as unknown:
        await _local(
            core,
            "dashboard",
            "node.narrow",
            {"node_id": body["node_id"], "group": "rocket", "narrowed": True},
        )
    assert unknown.value.details["reason"] == "group"


async def test_a_paired_device_needs_the_pin_even_to_narrow(core: NoxCore, tmp_path: Path) -> None:
    body, _context = await _pair(core, tmp_path)
    with pytest.raises(IpcError) as refused:
        await _local(
            core,
            "device",
            "node.narrow",
            {"node_id": body["node_id"], "group": "notify", "narrowed": True},
        )
    assert refused.value.code == ERR_PERMISSION


async def test_narrowing_holds_across_a_reconnect(core: NoxCore, tmp_path: Path) -> None:
    body, context = await _pair(core, tmp_path)
    first = await _connect(body, context)
    server = asyncio.create_task(first.serve())

    async def known() -> bool:
        return "notify" in _groups(await _node(core))

    await _until(known)
    await _local(
        core,
        "dashboard",
        "node.narrow",
        {"node_id": body["node_id"], "group": "notify", "narrowed": True},
    )
    await first.ws.close()
    server.cancel()

    # A node forgets the core's narrowing with the connection; the core sends it again at once.
    again = await _connect(body, context)
    server = asyncio.create_task(again.serve())
    try:

        async def told() -> bool:
            return again.policies == [["notify"]]

        await _until(told)
        node = await _node(core)
        assert "notify.show" not in node["tools"] and "system.info" in node["tools"]
        assert _groups(node)["notify"] == (True, True)
    finally:
        await again.ws.close()
        server.cancel()


async def test_the_default_device_per_family_is_live_and_kept(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    node_id = body["node_id"]
    result = await _local(
        core, "dashboard", "node.defaults.set", {"group": "notify", "device": node_id}
    )
    notify = next(d for d in result["defaults"] if d["group"] == "notify")
    assert notify["device"] == node_id and notify["tools"] == ["notify.show"]
    # Live for the router, and written for the next start.
    assert core.config.devices.defaults["notify.show"] == node_id
    written = yaml.safe_load((tmp_path / "user.yaml").read_text(encoding="utf-8"))
    assert written["devices"]["defaults"]["notify.show"] == node_id

    listed = await _local(core, "dashboard", "node.list", {})
    assert next(d for d in listed["defaults"] if d["group"] == "notify")["device"] == node_id

    await _local(core, "dashboard", "node.defaults.set", {"group": "notify", "device": "auto"})
    assert core.config.devices.defaults["notify.show"] == "auto"

    with pytest.raises(IpcError):
        await _local(
            core, "dashboard", "node.defaults.set", {"group": "notify", "device": "f" * 32}
        )
    with pytest.raises(IpcError) as refused:
        await _local(core, "device", "node.defaults.set", {"group": "notify", "device": "core"})
    assert refused.value.code == ERR_PERMISSION
