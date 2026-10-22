"""The dashboard's side of a node against the real core: narrowing a capability group, clearing it
again, the device as the only place that widens, the default device per tool family - and who may
do which of these with or without the security PIN.

A scripted node (raw JSON-RPC over the real mutual-TLS listener) answers `tools/list` with what
it has enabled minus what the core narrowed, exactly like `nox.node.agent` does, says which groups
it enabled (or, as an older node, does not), and records every `nox/policy` it is sent - so a test
sees what reached the device, not only what the core believes. A paired phone talks to the real
network listener with its real token, so the PIN rules are the ones a phone meets.
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
from nox.ipc.protocol import Kind
from nox.node.policy import relevant_groups
from nox.node.protocol import NODE_PATH
from tests.integration.test_lan import _auth, _request, _with_pin
from tests.integration.test_lan import _connect as lan_connect
from tests.integration.test_lan import _pair as lan_pair
from tests.integration.test_nodes import _config, _local, _nodes, _pair
from tests.unit.node.conftest import greet

PIN = "4711"
TOOLS = {
    "system.info": ("system", "read"),
    "notify.show": ("notify", "low"),
}


@dataclass
class ScriptedNode:
    ws: ClientConnection
    #: What the device enabled locally.
    enabled: set[str] = field(default_factory=lambda: {"system", "notify"})
    #: Whether it says so with every list (a current node) or not (an older one).
    report: bool = True
    narrowed: set[str] = field(default_factory=set)
    policies: list[list[str]] = field(default_factory=list)

    def listing(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "_nox": {"group": group, "risk": risk}}
            for name, (group, risk) in TOOLS.items()
            if group in self.enabled and group not in self.narrowed
        ]

    def _extra(self) -> dict[str, Any]:
        return {"_nox": {"groups": sorted(self.enabled)}} if self.report else {}

    async def serve(self) -> None:
        with contextlib.suppress(Exception):
            async for frame in self.ws:
                message = json.loads(frame)
                method, msg_id = message.get("method"), message.get("id")
                if method == "tools/list":
                    result: Any = {"tools": self.listing(), **self._extra()}
                elif method == "nox/policy":
                    groups = list(message["params"]["narrow"])
                    self.policies.append(groups)
                    self.narrowed = set(groups)
                    result = {"offered": [t["name"] for t in self.listing()], **self._extra()}
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


async def _connect(
    body: dict[str, Any], context: ssl.SSLContext, **node: Any
) -> tuple[ScriptedNode, asyncio.Task[None]]:
    ws = await connect(
        f"wss://127.0.0.1:{body['node_port']}{NODE_PATH}",
        ssl=context,
        server_hostname="localhost",
        open_timeout=5,
    )
    await greet(ws)
    scripted = ScriptedNode(ws, **node)
    return scripted, asyncio.create_task(scripted.serve())


async def _stop(node: ScriptedNode, task: asyncio.Task[None]) -> None:
    await node.ws.close()
    task.cancel()


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


def _narrow(node_id: str, group: str, narrowed: bool, **extra: Any) -> dict[str, Any]:
    return {"node_id": node_id, "group": group, "narrowed": narrowed, **extra}


def _audited(core: NoxCore, action: str) -> list[tuple[str, str]]:
    assert core.security is not None
    core.security.audit.flush(timeout_s=5.0)
    entries = core.security.audit_store.entries(limit=500)
    return [(e.decision, e.result) for e in entries if e.action == action]


async def _listed(core: NoxCore, tool: str = "notify.show") -> None:
    async def check() -> bool:
        return tool in (await _node(core))["tools"]

    await _until(check)


# ---- narrowing and clearing ---------------------------------------------------------------------


async def test_narrowing_reaches_the_node_and_the_core_routes_nothing_there(
    core: NoxCore, tmp_path: Path
) -> None:
    body, context = await _pair(core, tmp_path)
    node, task = await _connect(body, context)
    try:
        await _listed(core)
        first = await _node(core)
        groups = _groups(first)
        # Every group Nox can route is shown; this release decides which those are.
        assert set(groups) == relevant_groups()
        assert groups["system"] == (True, False) and groups["notify"] == (True, False)
        assert all(groups[g] == (False, False) for g in relevant_groups() - {"system", "notify"})
        assert first["os"] == "linux" and first["arch"] == "x86_64"

        result = await _local(
            core, "dashboard", "node.narrow", _narrow(body["node_id"], "notify", True)
        )
        assert result["ok"]
        assert node.policies[-1] == ["notify"]
        after = await _node(core)
        assert "notify.show" not in after["tools"]
        assert _groups(after)["notify"] == (True, True)
        registry = _nodes(core).registry
        assert registry is not None
        assert not registry.any_online_with("notify.show")

        await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "notify", False))
        assert node.policies[-1] == []
        await _listed(core)
        assert _groups(await _node(core))["notify"] == (True, False)
        assert _audited(core, "node.narrow")[-2:] == [("allow", "narrowed"), ("allow", "cleared")]
    finally:
        await _stop(node, task)


async def test_a_group_off_on_the_device_cannot_be_switched_on_here(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    with pytest.raises(IpcError) as refused:
        await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "shell", False))
    assert refused.value.code == ERR_VALIDATION
    assert refused.value.details["reason"] == "enable_on_device"
    with pytest.raises(IpcError) as unknown:
        await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "rocket", True))
    assert unknown.value.details["reason"] == "group"
    with pytest.raises(IpcError) as gone:
        await _local(core, "dashboard", "node.narrow", _narrow("f" * 32, "notify", True))
    assert gone.value.details["reason"] == "no_node"
    # Refusals are audited too.
    audited = _audited(core, "node.narrow")
    assert ("deny", "enable_on_device") in audited and ("deny", "no_node") in audited
    with pytest.raises(IpcError):
        await _local(
            core, "dashboard", "node.narrow", {**_narrow(body["node_id"], "notify", True), "x": 1}
        )


async def test_a_narrowed_group_can_always_be_cleared(core: NoxCore, tmp_path: Path) -> None:
    """An older node does not say what it enabled. Turned off on the device while narrowed here,
    the group must still be clearable - clearing never widens beyond the device."""
    body, context = await _pair(core, tmp_path)
    node, task = await _connect(body, context, report=False)
    await _listed(core)
    await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "notify", True))
    await _stop(node, task)
    # Back without "notify" enabled: the first listing after connecting is complete.
    node, task = await _connect(body, context, report=False, enabled={"system"})
    try:

        async def told() -> bool:
            return node.policies == [["notify"]]

        await _until(told)
        assert _groups(await _node(core))["notify"] == (False, True)
        await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "notify", False))
        assert _groups(await _node(core))["notify"] == (False, False)
        assert "notify.show" not in (await _node(core))["tools"]
    finally:
        await _stop(node, task)


async def test_narrowing_holds_across_a_reconnect(core: NoxCore, tmp_path: Path) -> None:
    body, context = await _pair(core, tmp_path)
    first, task = await _connect(body, context)
    await _listed(core)
    await _local(core, "dashboard", "node.narrow", _narrow(body["node_id"], "notify", True))
    await _stop(first, task)

    # A node forgets the core's narrowing with the connection; the core sends it again at once.
    again, task = await _connect(body, context)
    try:

        async def told() -> bool:
            return again.policies == [["notify"]]

        await _until(told)
        node = await _node(core)
        assert "notify.show" not in node["tools"] and "system.info" in node["tools"]
        assert _groups(node)["notify"] == (True, True)
    finally:
        await _stop(again, task)


# ---- the PIN ------------------------------------------------------------------------------------


async def test_with_a_pin_this_machine_narrows_freely_but_widens_only_with_it(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    _with_pin(core, PIN)
    node_id = body["node_id"]
    # Taking away needs nothing on the machine Nox runs on.
    await _local(core, "dashboard", "node.narrow", _narrow(node_id, "notify", True))
    with pytest.raises(IpcError) as refused:
        await _local(core, "dashboard", "node.narrow", _narrow(node_id, "notify", False))
    assert refused.value.code == ERR_PERMISSION
    with pytest.raises(IpcError):
        await _local(
            core, "dashboard", "node.narrow", _narrow(node_id, "notify", False, pin="0000")
        )
    await _local(core, "dashboard", "node.narrow", _narrow(node_id, "notify", False, pin=PIN))
    assert _groups(await _node(core))["notify"][1] is False


async def test_with_a_pin_defaults_change_only_with_it(core: NoxCore, tmp_path: Path) -> None:
    body, _context = await _pair(core, tmp_path)
    _with_pin(core, PIN)
    request = {"group": "notify", "device": body["node_id"]}
    with pytest.raises(IpcError) as refused:
        await _local(core, "dashboard", "node.defaults.set", request)
    assert refused.value.code == ERR_PERMISSION
    assert core.config.devices.defaults.get("notify.show") != body["node_id"]
    await _local(core, "dashboard", "node.defaults.set", {**request, "pin": PIN})
    assert core.config.devices.defaults["notify.show"] == body["node_id"]


async def test_a_phone_needs_the_right_pin_and_wrong_ones_remove_it(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    _with_pin(core, PIN)
    node_id = body["node_id"]
    phone = await lan_pair(core, "Handy")
    async with await lan_connect(core) as ws:
        await _auth(ws, token=phone["token"], role="device", client_id=phone["client_id"])

        async def ask(name: str, payload: dict[str, Any]) -> Any:
            return await _request(ws, phone["client_id"], name, payload)

        # Even taking away needs the PIN from a phone.
        without = await ask("node.narrow", _narrow(node_id, "notify", True))
        assert without.kind is Kind.ERROR and without.payload["code"] == ERR_PERMISSION
        right = await ask("node.narrow", _narrow(node_id, "notify", True, pin=PIN))
        assert right.kind is Kind.RESPONSE and right.payload["ok"] is True
        defaults = await ask("node.defaults.set", {"group": "notify", "device": "core", "pin": PIN})
        assert defaults.kind is Kind.RESPONSE
        assert core.config.devices.defaults["notify.show"] == "core"

        reasons = []
        for _ in range(3):
            wrong = await ask("node.narrow", _narrow(node_id, "notify", False, pin="0000"))
            assert wrong.kind is Kind.ERROR
            reasons.append(wrong.payload["details"]["reason"])
        # The device PIN guard counts them across requests and removes the phone.
        assert reasons == ["pin_wrong", "pin_wrong", "removed"]
    assert _groups(await _node(core))["notify"] == (False, True)


# ---- defaults ------------------------------------------------------------------------------------


async def test_the_default_device_per_family_is_live_and_kept(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    node_id = body["node_id"]
    live = core.config.devices.defaults
    result = await _local(
        core, "dashboard", "node.defaults.set", {"group": "notify", "device": node_id}
    )
    notify = next(d for d in result["defaults"] if d["group"] == "notify")
    assert notify["device"] == node_id and notify["tools"] == ["notify.show"]
    # Live for the router (the same mapping it reads), and written for the next start.
    assert core.config.devices.defaults is live and live["notify.show"] == node_id
    written = yaml.safe_load((tmp_path / "user.yaml").read_text(encoding="utf-8"))
    assert written["devices"]["defaults"]["notify.show"] == node_id

    listed = await _local(core, "dashboard", "node.list", {})
    assert next(d for d in listed["defaults"] if d["group"] == "notify")["device"] == node_id

    await _local(core, "dashboard", "node.defaults.set", {"group": "notify", "device": "auto"})
    assert live["notify.show"] == "auto"

    with pytest.raises(IpcError):
        await _local(
            core, "dashboard", "node.defaults.set", {"group": "notify", "device": "f" * 32}
        )
    with pytest.raises(IpcError) as refused:
        await _local(core, "device", "node.defaults.set", {"group": "notify", "device": "core"})
    assert refused.value.code == ERR_PERMISSION
    assert ("deny", "no_node") in _audited(core, "node.defaults")


async def test_removing_a_node_resets_the_defaults_that_named_it(
    core: NoxCore, tmp_path: Path
) -> None:
    body, _context = await _pair(core, tmp_path)
    node_id = body["node_id"]
    await _local(core, "dashboard", "node.defaults.set", {"group": "notify", "device": node_id})
    await _local(core, "dashboard", "node.unpair", {"device_id": node_id})
    assert core.config.devices.defaults["notify.show"] == "auto"
    written = yaml.safe_load((tmp_path / "user.yaml").read_text(encoding="utf-8"))
    assert written["devices"]["defaults"]["notify.show"] == "auto"
