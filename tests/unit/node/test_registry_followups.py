"""Registry follow-ups: list changes coalesced per connection, waiting only for the devices a
call can land on, and a failed first tool list retried instead of leaving a node empty."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from nox.mcp.client import McpClientError
from nox.node import registry as registry_module
from nox.node.pairing import NodePairing
from nox.node.registry import NodeEntry, NodeRegistry
from nox.node.repo import NodeRepository
from nox.node.router import resolve, routed_spec, waits_for
from nox.node.tools import NotifyArgs
from nox.security.model import Risk
from nox.tools.registry import ToolSpec
from tests.unit.node.conftest import pair_node
from tests.unit.node.test_policy import _Client, _Session, _SlowListing

NOTIFY = [{"name": "notify.show", "_nox": {"group": "notify", "risk": "low"}}]


def _listings(client: _Client) -> int:
    return sum(1 for method, _ in client.sent if method == "tools/list")


async def test_a_change_from_a_new_connection_is_not_swallowed_by_the_old_one(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    old_client = _SlowListing(NOTIFY)
    old_client.answer.set()
    old = _Session(pairing.get(node.node_id), old_client)
    await registry.on_connected(old)  # type: ignore[arg-type]
    old_client.answer.clear()
    # The old connection is in the middle of a listing for a change ...
    running = asyncio.create_task(registry.on_tools_changed(old))  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    # ... when the node reconnects and reports a change on the new connection.
    new_client = _Client(NOTIFY)
    new = _Session(pairing.get(node.node_id), new_client)
    entry = next(e for e in registry.entries() if e.device_id == node.node_id)
    entry.session = new  # type: ignore[assignment]
    changed = asyncio.create_task(registry.on_tools_changed(new))  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    old_client.answer.set()
    await asyncio.gather(running, changed)
    assert _listings(new_client) == 1


def _entry(device_id: str, name: str, *, connecting: bool, linked: str | None = None) -> NodeEntry:
    entry = NodeEntry(device_id=device_id, name=name, online=True, linked_device_id=linked)
    if connecting:
        entry.listed = asyncio.Event()
    return entry


async def test_a_call_waits_only_for_the_devices_it_can_land_on() -> None:
    laptop = _entry("a" * 32, "Laptop", connecting=True, linked="browser-1")
    pi = _entry("b" * 32, "Wohnzimmer-Pi", connecting=True)
    pc = _entry("c" * 32, "Arbeits-PC", connecting=False)
    entries = [laptop, pi, pc]

    def wait(requested: str | None, default: str | None, *, core_has: bool = True) -> list[str]:
        return waits_for(
            requested, entries=entries, core_has=core_has, default=default, origin="browser-1"
        )

    both = sorted([laptop.device_id, pi.device_id])
    assert wait("core", None) == []
    assert wait("Laptop", None) == [laptop.device_id]
    assert wait("Arbeits-PC", None) == []  # listed already
    assert wait("hier", None) == [laptop.device_id]
    assert wait(None, "core") == []
    assert wait(None, "core", core_has=False) == both
    assert wait(None, "origin") == [laptop.device_id]
    assert wait(None, pi.device_id) == [pi.device_id]
    assert wait(None, "auto") == both
    assert wait(None, None) == both


async def test_a_core_call_is_never_delayed_by_a_slow_node(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    slow = _SlowListing(NOTIFY)  # does not answer its first list until the end of the test
    session = _Session(pairing.get(node.node_id), slow)
    connecting = asyncio.create_task(registry.on_connected(session))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)

    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"shown": True}

    core = ToolSpec(
        name="notify.show", description="t", input_model=NotifyArgs, risk=Risk.LOW, handler=handler
    )
    spec = routed_spec(
        name="notify.show",
        description="t",
        risk=Risk.LOW,
        side_effects=True,
        registry=registry,
        core=core,
    )
    assert spec.prepare is not None

    def no_database(*_a: Any) -> list[NodeEntry]:
        raise AssertionError("prepare must not read the database")

    registry.entries = no_database  # type: ignore[method-assign]
    started = time.monotonic()
    await spec.prepare({"device": "core", "text": "x"})
    await registry.settle(["f" * 32])  # an unrelated device: no wait either
    assert time.monotonic() - started < 0.5
    slow.answer.set()
    await connecting


class _FailsFirst(_Client):
    def __init__(self, tools: list[dict[str, Any]], failures: int) -> None:
        super().__init__(tools)
        self.failures = failures

    async def request(self, method: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if method == "tools/list" and self.failures > 0:
            self.failures -= 1
            self.sent.append((method, params))
            raise McpClientError("no answer")
        return await super().request(method, params, **kw)


async def _until(check: Any) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.01)


async def test_a_failed_first_listing_is_retried(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.01, 0.01, 0.01))
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _FailsFirst(NOTIFY, failures=2)
    await registry.on_connected(_Session(pairing.get(node.node_id), client))  # type: ignore[arg-type]
    assert not registry.any_online_with("notify.show")
    await _until(lambda: registry.any_online_with("notify.show"))
    assert registry.any_online_with("notify.show")
    assert _listings(client) == 3


async def test_retries_end_and_leave_the_rest_to_the_next_change(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.01, 0.01))
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _FailsFirst(NOTIFY, failures=10)
    session = _Session(pairing.get(node.node_id), client)
    await registry.on_connected(session)  # type: ignore[arg-type]
    await _until(lambda: not registry._retries)
    assert not registry._retries
    assert _listings(client) == 3  # the first and two retries, then it stops
    client.failures = 0
    await registry.on_tools_changed(session)  # type: ignore[arg-type]
    assert registry.any_online_with("notify.show")


async def test_no_retry_for_a_connection_that_is_gone(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.05,))
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _FailsFirst(NOTIFY, failures=1)
    old = _Session(pairing.get(node.node_id), client)
    await registry.on_connected(old)  # type: ignore[arg-type]
    await registry.on_disconnected(old)  # type: ignore[arg-type]
    await asyncio.sleep(0.1)
    assert _listings(client) == 1


async def test_a_retry_after_a_policy_was_sent_is_not_a_complete_listing(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older node (no `_nox.groups`): once the core told this connection to narrow "notify",
    the node stops listing it - that says nothing about the device, which still has it on."""
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.05,))
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    both = [*NOTIFY, {"name": "system.info", "_nox": {"group": "system", "risk": "read"}}]
    first = _Client(both)
    await registry.on_connected(_Session(pairing.get(node.node_id), first))  # type: ignore[arg-type]
    assert registry.policy.state(node.node_id, "notify") == (True, False)

    class Narrowing(_FailsFirst):
        """Fails its first list; afterwards lists what is not narrowed, like the agent."""

        narrow: list[str] = []

        async def request(self, method: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
            if method == "nox/policy":
                self.narrow = list(params["narrow"])
                self.tools = [t for t in both if t["_nox"]["group"] not in self.narrow]
            return await super().request(method, params, **kw)

    client = Narrowing(both, failures=1)
    session = _Session(pairing.get(node.node_id), client)
    await registry.on_connected(session)  # type: ignore[arg-type]
    await registry.set_narrowed(node.node_id, "notify", True)  # sent on this connection
    await _until(lambda: _listings(client) >= 2 and not registry._retries)
    assert registry.policy.state(node.node_id, "notify") == (True, True)


async def test_retries_are_cancelled_when_the_registry_closes(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.2, 0.2))
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _FailsFirst(NOTIFY, failures=5)
    await registry.on_connected(_Session(pairing.get(node.node_id), client))  # type: ignore[arg-type]
    assert registry._retries
    await registry.close()
    assert not registry._retries
    await asyncio.sleep(0.3)
    assert _listings(client) == 1


async def test_a_call_naming_a_node_still_announcing_is_told_so(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "LISTING_RETRY_S", (0.05,))
    node = await pair_node(pairing, tmp_path, name="Laptop")
    registry = NodeRegistry(pairing, nodes)
    client = _FailsFirst(NOTIFY, failures=1)
    await registry.on_connected(_Session(pairing.get(node.node_id), client))  # type: ignore[arg-type]

    def ask() -> Any:
        return resolve(
            "notify.show",
            "Laptop",
            entries=registry.entries(),
            core_has=False,
            side_effects=True,
            default=None,
            origin=None,
        )

    waiting = ask()
    assert waiting.error is not None and waiting.error["error"] == "device_listing"
    await _until(lambda: registry.any_online_with("notify.show"))
    assert ask().device == node.node_id
