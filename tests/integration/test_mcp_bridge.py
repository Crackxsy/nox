"""The MCP bridge end to end: a real hub, the core-side gate, and `python -m nox.mcp.bridge` as a
subprocess speaking MCP over stdio - the way Claude Code drives it, without Claude Code.

Every security control of the bridge has a negative test here that fails without the control:
a call without an open turn, with the token of a turn that ended, from the wrong role, with a
token issued for another role, for a tool outside the turn's offer, and after the budget ran out.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.ai.tooluse import OfferedTool
from nox.core.bus import AsyncEventBus
from nox.core.toolloop import ToolLoop
from nox.core.turnbudget import TurnBudget
from nox.ipc.client import IpcClient
from nox.ipc.dispatch import RequestRegistry
from nox.ipc.errors import ERR_AUTH_DENIED, ERR_PERMISSION, ERR_VALIDATION, IpcError
from nox.ipc.server import HubSettings, IpcHub
from nox.ipc.tokens import TokenStore
from nox.mcp.gate import McpGate, McpLaunch

LIGHT = OfferedTool(
    name="home.light",
    description="Switch a light.",
    schema={"type": "object", "properties": {"on": {"type": "boolean"}}},
)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Outcome(BaseModel):
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.data: dict[str, Any] = {"state": "on"}

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Outcome:
        self.calls.append((name, arguments))
        return Outcome(ok=True, data=self.data)


class World:
    def __init__(self, hub: IpcHub, gate: McpGate, loop: ToolLoop, recorder: Recorder) -> None:
        self.hub, self.gate, self.loop, self.recorder = hub, gate, loop, recorder
        self.tokens: TokenStore = hub._tokens  # noqa: SLF001 - the test's own store
        self.cloud_allowed = True
        self.lost: list[str] = []


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    bus = AsyncEventBus()
    tokens = TokenStore()
    registry = RequestRegistry()
    hub = IpcHub(HubSettings(port=0), tokens, registry, bus, tmp_path)
    await hub.start()
    recorder = Recorder()
    loop = ToolLoop(
        offer=lambda: [LIGHT], call=recorder, budget_factory=lambda: TurnBudget(max_calls=2)
    )
    holder: list[World] = []
    gate = McpGate(
        hub=hub,
        hub_url=lambda: hub.url,
        tokens=tokens,
        bus=bus,
        tool_loop=lambda: loop,
        runtime_dir=tmp_path,
        command=[sys.executable],
        cloud_allowed=lambda: holder[0].cloud_allowed,
        on_bridge_lost=lambda client_id: holder[0].lost.append(client_id),
    )
    gate.register(registry)
    world = World(hub, gate, loop, recorder)
    holder.append(world)
    yield world
    await gate.close()
    await hub.stop()


class Bridge:
    """The bridge subprocess, driven over MCP stdio like Claude Code drives it."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self._next = 0

    @classmethod
    async def start(cls, launch: McpLaunch) -> Bridge:
        server = json.loads(launch.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"]
        proc = await asyncio.create_subprocess_exec(
            server["command"],
            *server["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **server["env"]},
            creationflags=NO_WINDOW,
        )
        return cls(proc)

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._next += 1
        message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
        assert line, f"bridge ended: {(await self.proc.stderr.read()).decode()!r}"  # type: ignore[union-attr]
        reply: dict[str, Any] = json.loads(line)
        assert reply["id"] == self._next
        return reply

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = await self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        result: dict[str, Any] = reply["result"]
        return result

    async def close(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


async def _connected(world: World, launch: McpLaunch) -> Bridge:
    bridge = await Bridge.start(launch)
    init = await bridge.rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["serverInfo"]["name"] == "nox"
    return bridge


async def _until(predicate: Any, timeout_s: float = 10.0) -> None:
    """Poll a condition on the event loop (no fixed sleep): the bridge is another process."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


async def test_tools_are_listed_in_wire_names_and_calls_run_through_the_loop(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        listed = await bridge.rpc("tools/list")
        assert [t["name"] for t in listed["result"]["tools"]] == ["home-light"]

        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)
        result = await bridge.call("home-light", {"on": True})

        assert result["isError"] is False
        assert "Result of home.light" in result["content"][0]["text"]
        assert world.recorder.calls == [("home.light", {"on": True})]
        assert turn.used == ["home.light"]
    finally:
        await bridge.close()


async def test_results_with_any_unicode_reach_the_caller(world: World) -> None:
    """Window titles carry emoji and dashes; the console code page cannot encode them."""
    world.recorder.data = {"windows": ["Grüße – Stream 🎮", "日本語"]}
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)

        result = await bridge.call("home-light")

        assert result["isError"] is False
        assert "Grüße – Stream 🎮" in result["content"][0]["text"]
    finally:
        await bridge.close()


async def test_the_config_file_is_gone_once_the_bridge_is_in(world: World) -> None:
    launch = world.gate.prepare()
    assert launch.config_path.exists()
    bridge = await _connected(world, launch)
    try:
        await _until(lambda: not launch.config_path.exists())
    finally:
        await bridge.close()


async def test_a_call_without_an_open_turn_is_refused(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        result = await bridge.call("home-light")

        assert result["isError"] is True
        assert world.recorder.calls == []
    finally:
        await bridge.close()


async def test_the_token_of_a_turn_that_ended_is_refused(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)
        await world.loop.end(turn)

        result = await bridge.call("home-light")

        assert result["isError"] is True
        assert "no open turn" in result["content"][0]["text"]
        assert world.recorder.calls == []
    finally:
        await bridge.close()


async def test_a_tool_outside_the_offer_never_reaches_the_executor(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)

        result = await bridge.call("file-delete", {"path": "C:/"})

        assert result["isError"] is True
        assert "tool.not_offered" in result["content"][0]["text"]
        assert world.recorder.calls == []
    finally:
        await bridge.close()


async def test_a_spent_budget_refuses_further_calls(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)

        results = [await bridge.call("home-light", {"on": i % 2 == 0}) for i in range(3)]

        assert [r["isError"] for r in results] == [False, False, True]
        assert "turn.budget" in results[2]["content"][0]["text"]
        assert len(world.recorder.calls) == 2
    finally:
        await bridge.close()


async def test_retiring_the_bridge_ends_its_process(world: World) -> None:
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        await world.gate.retire(launch)
        assert bridge.proc.stdin is not None
        bridge.proc.stdin.close()
        await asyncio.wait_for(bridge.proc.wait(), timeout=10)
    finally:
        await bridge.close()


# -- the IPC door itself, without the bridge process -------------------------------------------


async def _client(world: World, token: str, role: str, client_id: str) -> IpcClient:
    client = IpcClient(world.hub.url, token, role, client_id)
    await client.connect()
    return client


async def test_another_role_cannot_call_the_mcp_requests(world: World) -> None:
    client = await _client(world, world.tokens.session_token, "dashboard", "dash")
    try:
        turn = world.loop.begin("t1")
        with pytest.raises(IpcError) as caught:
            await client.request(
                "mcp.tools.call", {"token": turn.token, "name": "home-light", "arguments": {}}
            )
        assert caught.value.code == ERR_PERMISSION
        assert world.recorder.calls == []
    finally:
        await client.close()


async def test_a_worker_token_does_not_open_the_mcp_role(world: World) -> None:
    token = world.tokens.issue_worker_token("mcp:forged")
    with pytest.raises(IpcError) as caught:
        await _client(world, token, "mcp", "mcp:forged")
    assert caught.value.code == ERR_AUTH_DENIED


async def test_an_mcp_token_does_not_open_the_worker_role(world: World) -> None:
    launch = world.gate.prepare()
    env = json.loads(launch.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"]["env"]
    with pytest.raises(IpcError) as caught:
        await _client(world, env["NOX_WORKER_TOKEN"], "worker", launch.client_id)
    assert caught.value.code == ERR_AUTH_DENIED


async def test_an_mcp_client_the_gate_did_not_start_is_refused(world: World) -> None:
    token = world.tokens.issue_worker_token("mcp:stray", role="mcp")
    client = await _client(world, token, "mcp", "mcp:stray")
    try:
        with pytest.raises(IpcError) as caught:
            await client.request("mcp.tools.list", {})
        assert caught.value.code == ERR_PERMISSION
    finally:
        await client.close()


async def test_a_call_without_a_token_fails_validation(world: World) -> None:
    launch = world.gate.prepare()
    env = json.loads(launch.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"]["env"]
    client = await _client(world, env["NOX_WORKER_TOKEN"], "mcp", launch.client_id)
    try:
        world.loop.begin("t1")
        with pytest.raises(IpcError) as caught:
            await client.request("mcp.tools.call", {"name": "home-light", "arguments": {}})
        assert caught.value.code == ERR_VALIDATION
        assert world.recorder.calls == []
    finally:
        await client.close()


async def test_the_mcp_role_cannot_subscribe_to_events(world: World) -> None:
    """Chat, transcripts and memory must never reach a process a language model drives."""
    launch = world.gate.prepare()
    env = json.loads(launch.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"]["env"]
    client = await _client(world, env["NOX_WORKER_TOKEN"], "mcp", launch.client_id)
    try:
        with pytest.raises(IpcError) as caught:
            await client.subscribe(["chat.*", "voice.*", "memory.*"])
        assert caught.value.code == ERR_PERMISSION
    finally:
        await client.close()


async def test_a_token_that_is_not_the_open_turn_s_is_refused(world: World) -> None:
    """Possession of the bridge's connection is not enough: the call must name its turn."""
    launch = world.gate.prepare()
    env = json.loads(launch.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"]["env"]
    client = await _client(world, env["NOX_WORKER_TOKEN"], "mcp", launch.client_id)
    try:
        world.loop.begin("open-turn")
        with pytest.raises(IpcError) as caught:
            await client.request(
                "mcp.tools.call", {"token": "x" * 43, "name": "home-light", "arguments": {}}
            )
        assert caught.value.code == ERR_PERMISSION
        assert world.recorder.calls == []
    finally:
        await client.close()


def test_the_mcp_requests_are_registered_for_the_mcp_role_only(world: World) -> None:
    registry = world.hub.registry
    for name in ("mcp.tools.list", "mcp.tools.call"):
        registration = registry.get(name)
        assert registration is not None and registration.allowed_roles == frozenset({"mcp"})


async def test_switching_to_private_mid_turn_refuses_the_next_call(world: World) -> None:
    """Claude Code is a cloud model: from the moment the privacy mode forbids the cloud, no tool
    result may reach it any more, even inside a turn that started in balanced mode."""
    launch = world.gate.prepare()
    bridge = await _connected(world, launch)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(launch, turn.token)
        first = await bridge.call("home-light", {"on": True})

        world.cloud_allowed = False
        second = await bridge.call("home-light", {"on": False})

        assert first["isError"] is False
        assert second["isError"] is True and "privacy" in second["content"][0]["text"]
        assert len(world.recorder.calls) == 1
    finally:
        await bridge.close()


async def test_a_turn_token_works_only_for_the_bridge_it_was_handed_to(world: World) -> None:
    own, other = world.gate.prepare(), world.gate.prepare()
    other_env = json.loads(other.config_path.read_text(encoding="utf-8"))["mcpServers"]["nox"][
        "env"
    ]
    intruder = await _client(world, other_env["NOX_WORKER_TOKEN"], "mcp", other.client_id)
    try:
        turn = world.loop.begin("t1")
        await world.gate.activate(own, turn.token)

        with pytest.raises(IpcError) as caught:
            await intruder.request(
                "mcp.tools.call", {"token": turn.token, "name": "home-light", "arguments": {}}
            )

        assert caught.value.code == ERR_PERMISSION
        assert world.recorder.calls == []
    finally:
        await intruder.close()


async def test_an_unexpected_bridge_loss_is_reported_and_a_retired_one_is_not(
    world: World,
) -> None:
    lost, retired = world.gate.prepare(), world.gate.prepare()
    lost_bridge = await _connected(world, lost)
    retired_bridge = await _connected(world, retired)

    await lost_bridge.close()  # the process dies on its own
    await _until(lambda: world.lost == [lost.client_id])
    await world.gate.retire(retired)
    await retired_bridge.close()

    assert world.lost == [lost.client_id]


async def test_the_bridge_does_not_announce_tool_list_changes(world: World) -> None:
    launch = world.gate.prepare()
    real = await Bridge.start(launch)
    try:
        init = await real.rpc("initialize", {"protocolVersion": "2025-06-18"})
        assert init["result"]["capabilities"]["tools"] == {"listChanged": False}
    finally:
        await real.close()
