"""`nox.node.policy`: the core's own tool -> group mapping, what a listing records, and that a
narrowed group stays out of what the core routes - whatever the node says."""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from nox.node.manifest import GROUPS
from nox.node.pairing import NodePairing
from nox.node.policy import NodePolicy, defaults_view, family_tools, group_of, reported_groups
from nox.node.registry import ROUTABLE, NodeEntry, NodeRegistry
from nox.node.repo import NodeRepository
from nox.node.router import resolve
from tests.unit.node.conftest import pair_node


def test_every_routable_tool_belongs_to_a_known_group() -> None:
    for tool in ROUTABLE:
        group = group_of(tool)
        assert group in GROUPS, tool
    assert group_of("shell.run") == "shell"
    assert group_of("package.install") == "packages"
    assert group_of("file.read") == "files"
    assert group_of("game.input.send") is None


def test_family_tools_include_configured_ones() -> None:
    assert family_tools("notify", ROUTABLE, ["shell.run"]) == ["notify.show"]
    assert family_tools("shell", ["notify.show"], ["shell.run"]) == ["shell.run"]


async def test_a_listing_records_groups_and_filters_narrowed_ones(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    policy = NodePolicy(nodes)
    tools = {"system.info": "read", "notify.show": "low"}
    assert policy.record_listing(node.node_id, tools) == tools
    policy.set_narrowed(node.node_id, "notify", True)
    assert policy.record_listing(node.node_id, tools) == {"system.info": "read"}
    # While narrowed the node no longer lists the group; that says nothing about the device.
    assert policy.record_listing(node.node_id, {"system.info": "read"}) == {"system.info": "read"}
    view = {g["group"]: g for g in policy.view(node.node_id, ["system"])}
    assert view["notify"]["enabled_on_device"] and view["notify"]["narrowed"]
    assert not view["notify"]["active"]
    assert view["system"]["active"]
    # A group the device never reported is shown only when Nox can route it.
    assert "shell" not in view
    assert "shell" in {g["group"] for g in policy.view(node.node_id, ["shell"])}
    assert policy.state(node.node_id, "shell") == (False, False)
    # Right after connecting the listing is complete: a narrowed group it lacks is off there.
    policy.record_listing(node.node_id, {"system.info": "read"}, complete=True)
    assert policy.state(node.node_id, "notify") == (False, True)


async def test_what_the_node_reports_is_authoritative(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    policy = NodePolicy(nodes)
    policy.set_narrowed(node.node_id, "notify", True)
    # Shell is enabled on the device but above its max_risk, so no shell tool is listed.
    policy.record_listing(
        node.node_id, {"system.info": "read"}, reported={"system", "notify", "shell"}
    )
    assert policy.state(node.node_id, "shell") == (True, False)
    assert policy.state(node.node_id, "notify") == (True, True)
    policy.record_listing(node.node_id, {"system.info": "read"}, reported={"system"})
    assert policy.state(node.node_id, "notify") == (False, True)
    assert reported_groups({"_nox": {"groups": ["system", "rocket", 3]}}) == {"system"}
    assert reported_groups({"tools": []}) is None


async def test_a_tool_of_no_known_group_is_never_routed(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    accepted = NodePolicy(nodes).record_listing(
        node.node_id, {"system.info": "read", "mystery.tool": "low"}
    )
    assert accepted == {"system.info": "read"}


class _Client:
    def __init__(self, tools: list[dict[str, Any]]) -> None:
        self.tools = tools
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.sent.append((method, params))
        if method == "tools/list":
            return {"tools": self.tools}
        return {}


class _Session:
    def __init__(self, device: Any, client: _Client) -> None:
        self.device = device
        self.client = client
        self.compatible = True
        self.alive = True

        class Hello:
            confirmer = False

        self.hello = Hello()

    @property
    def device_id(self) -> str:
        return str(self.device.id)


async def test_the_core_never_routes_a_narrowed_group_even_if_the_node_lists_it(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    registry.policy.set_narrowed(node.node_id, "notify", True)
    # A node that ignores `nox/policy` and keeps listing the group - or claims it is "system".
    client = _Client(
        [
            {"name": "system.info", "_nox": {"group": "system", "risk": "read"}},
            {"name": "notify.show", "_nox": {"group": "system", "risk": "low"}},
        ]
    )
    session = _Session(pairing.get(node.node_id), client)
    await registry.on_connected(session)  # type: ignore[arg-type]
    assert not registry.any_online_with("notify.show")
    assert registry.any_online_with("system.info")
    assert ("nox/policy", {"narrow": ["notify"]}) in client.sent

    # Giving it back tells the node first, then lists again.
    await registry.set_narrowed(node.node_id, "notify", False)
    assert client.sent[-2] == ("nox/policy", {"narrow": []})
    assert client.sent[-1][0] == "tools/list"
    assert registry.any_online_with("notify.show")

    # Narrowing takes effect on the core before the node answers anything.
    await registry.set_narrowed(node.node_id, "notify", True)
    assert not registry.any_online_with("notify.show")


def test_a_group_switched_off_in_the_dashboard_says_so() -> None:
    pc = NodeEntry(
        device_id="a" * 32,
        name="Arbeits-PC",
        online=True,
        tools={"system.info": "read"},
        narrowed=frozenset({"notify"}),
    )
    common: dict[str, Any] = {"entries": [pc], "core_has": False, "default": None, "origin": None}
    narrowed = resolve("notify.show", "Arbeits-PC", side_effects=True, **common)
    assert narrowed.error is not None and narrowed.error["error"] == "narrowed_on_core"
    off = resolve("shell.run", "Arbeits-PC", side_effects=True, **common)
    assert off.error is not None and off.error["error"] == "not_enabled_on_device"


def test_auto_means_no_default() -> None:
    laptop = NodeEntry(device_id="a" * 32, name="auto", online=True, tools={"notify.show": "low"})
    pi = NodeEntry(device_id="b" * 32, name="Pi", online=True, tools={"notify.show": "low"})
    result = resolve(
        "notify.show",
        None,
        entries=[laptop, pi],
        core_has=False,
        side_effects=True,
        default="auto",
        origin=None,
    )
    # Not the node that happens to be called "auto": two candidates, so it asks.
    assert result.device == "" and result.error is not None
    assert result.error["error"] == "needs_device"


async def test_the_defaults_view_resolves_names_to_ids(
    pairing: NodePairing, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path, name="Laptop")
    listed = pairing.nodes()
    view = {d["group"]: d for d in defaults_view({"notify.show": "laptop"}, listed)}
    assert view["notify"]["device"] == node.node_id
    assert view["notify"]["raw"] == "laptop"
    assert view["system"]["device"] == "auto"
    unknown = {d["group"]: d for d in defaults_view({"notify.show": "Kitchen PC"}, listed)}
    assert unknown["notify"]["device"] == "Kitchen PC"


async def _until_set(flag: threading.Event) -> None:
    for _ in range(500):
        if flag.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the worker thread never got there")


async def _never_offered_while(registry: NodeRegistry, *tasks: asyncio.Task[None]) -> None:
    while not all(t.done() for t in tasks):
        assert not registry.any_online_with("notify.show")
        await asyncio.sleep(0.001)
    for task in tasks:
        task.result()


async def _connected(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> tuple[NodeRegistry, _Session, str]:
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _Client(
        [
            {"name": "system.info", "_nox": {"group": "system", "risk": "read"}},
            {"name": "notify.show", "_nox": {"group": "notify", "risk": "low"}},
        ]
    )
    session = _Session(pairing.get(node.node_id), client)
    await registry.on_connected(session)  # type: ignore[arg-type]
    assert registry.any_online_with("notify.show")
    return registry, session, node.node_id


async def test_a_listing_racing_a_narrowing_never_routes_the_group(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    registry, session, node_id = await _connected(pairing, nodes, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = registry.policy.record_listing

    def slow(*args: Any, **kwargs: Any) -> dict[str, str]:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    registry.policy.record_listing = slow  # type: ignore[method-assign]
    # A listing (still with "notify") is in the middle of being recorded ...
    listing = asyncio.create_task(registry.refresh(session))  # type: ignore[arg-type]
    await _until_set(entered)
    # ... when the owner switches "notify" off: off at once, and never back on.
    narrowing = asyncio.create_task(registry.set_narrowed(node_id, "notify", True))
    await asyncio.sleep(0)
    assert not registry.any_online_with("notify.show")
    release.set()
    await _never_offered_while(registry, listing, narrowing)
    assert registry.policy.narrowed(node_id) == {"notify"}
    entry = next(e for e in registry.entries() if e.device_id == node_id)
    assert entry.narrowed == {"notify"} and "notify.show" not in entry.tools
    assert session.client.sent[-1] == ("nox/policy", {"narrow": ["notify"]})


async def test_a_narrowing_racing_a_listing_never_routes_the_group(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    registry, session, node_id = await _connected(pairing, nodes, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = registry.policy.set_narrowed

    def slow(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    registry.policy.set_narrowed = slow  # type: ignore[method-assign]
    # The narrowing is being written ...
    narrowing = asyncio.create_task(registry.set_narrowed(node_id, "notify", True))
    await _until_set(entered)
    assert not registry.any_online_with("notify.show")
    # ... while the node reports a fresh list that still has "notify".
    listing = asyncio.create_task(registry.refresh(session))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    release.set()
    await _never_offered_while(registry, listing, narrowing)
    assert registry.policy.narrowed(node_id) == {"notify"}
    assert not registry.any_online_with("notify.show")


def test_a_stale_tool_list_never_routes_a_narrowed_or_unknown_group() -> None:
    entry = NodeEntry(
        device_id="a" * 32,
        name="Arbeits-PC",
        online=True,
        tools={"notify.show": "low", "mystery.tool": "low", "system.info": "read"},
        narrowed=frozenset({"notify"}),
    )
    assert not entry.offers("notify.show")
    assert not entry.offers("mystery.tool")
    assert entry.offers("system.info")
    entry.pending_narrow["system"] += 1
    assert not entry.offers("system.info")


ARRIVALS = sorted(set(itertools.permutations(["narrow", "narrow", "clear"])))


@pytest.mark.parametrize("arrivals", ARRIVALS, ids=["-".join(a) for a in ARRIVALS])
async def test_narrow_and_clear_requests_apply_in_arrival_order(
    pairing: NodePairing,
    nodes: NodeRepository,
    tmp_path: Path,
    arrivals: tuple[str, ...],
) -> None:
    registry, _session, node_id = await _connected(pairing, nodes, tmp_path)
    entry = next(e for e in registry.entries() if e.device_id == node_id)
    lock = registry._lock(node_id)
    await lock.acquire()  # everything queues behind a listing in progress
    tasks: list[tuple[str, asyncio.Task[None]]] = []
    for kind in arrivals:
        task = asyncio.create_task(registry.set_narrowed(node_id, "notify", kind == "narrow"))
        tasks.append((kind, task))
        await asyncio.sleep(0)  # it arrives (and is stamped) before the next one
    lock.release()
    while not all(t.done() for _, t in tasks):
        if any(kind == "narrow" and not t.done() for kind, t in tasks):
            # A narrowing on its way: off, whatever else already finished.
            assert not registry.any_online_with("notify.show")
        await asyncio.sleep(0.001)
    for _, task in tasks:
        task.result()
    narrowed = arrivals[-1] == "narrow"  # the last to arrive wins
    assert ("notify" in registry.policy.narrowed(node_id)) is narrowed
    assert registry.any_online_with("notify.show") is not narrowed
    assert entry.pending_narrow == Counter()


async def test_an_older_request_never_undoes_a_newer_one(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    registry, _session, node_id = await _connected(pairing, nodes, tmp_path)
    entry = next(e for e in registry.entries() if e.device_id == node_id)
    # As if a later "clear" had been applied before this narrowing got the lock.
    registry._applied[(node_id, "notify")] = 10**9
    await registry.set_narrowed(node_id, "notify", True)
    assert registry.policy.narrowed(node_id) == set()
    assert registry.any_online_with("notify.show")
    assert entry.pending_narrow == Counter()


async def test_the_marker_is_removed_on_an_error_and_a_cancellation(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    registry, _session, node_id = await _connected(pairing, nodes, tmp_path)
    entry = next(e for e in registry.entries() if e.device_id == node_id)

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    original = registry.policy.set_narrowed
    registry.policy.set_narrowed = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await registry.set_narrowed(node_id, "notify", True)
    assert entry.pending_narrow == Counter()
    registry.policy.set_narrowed = original  # type: ignore[method-assign]

    lock = registry._lock(node_id)
    await lock.acquire()
    waiting = asyncio.create_task(registry.set_narrowed(node_id, "notify", True))
    await asyncio.sleep(0)
    assert entry.pending_narrow["notify"] == 1
    assert not registry.any_online_with("notify.show")
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    lock.release()
    assert entry.pending_narrow == Counter()
    assert registry.any_online_with("notify.show")


class _SlowListing(_Client):
    """A node whose first `tools/list` answer takes a moment to arrive."""

    def __init__(self, tools: list[dict[str, Any]]) -> None:
        super().__init__(tools)
        self.answer = asyncio.Event()

    async def request(self, method: str, params: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if method == "tools/list":
            await self.answer.wait()
        return await super().request(method, params, **kw)


async def test_a_call_right_after_connecting_waits_for_the_tool_list(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _SlowListing([{"name": "notify.show", "_nox": {"group": "notify", "risk": "low"}}])
    session = _Session(pairing.get(node.node_id), client)
    connecting = asyncio.create_task(registry.on_connected(session))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    entry = next(e for e in registry.entries() if e.device_id == node.node_id)
    assert entry.connecting and not entry.offers("notify.show")
    # The call comes in the moment the session is up - before the list is recorded.
    call = asyncio.create_task(
        registry.call(entry, "notify.show", {"text": "Essen ist fertig"}, risk="low", timeout_s=5)
    )
    settled = asyncio.create_task(registry.settle())
    await asyncio.sleep(0.05)
    assert not call.done() and not settled.done()
    client.answer.set()
    result = await call
    await settled
    await connecting
    assert "error" not in result, result
    assert ("tools/call", "notify.show") in [(m, p.get("name")) for m, p in client.sent]
    assert registry.online_with("notify.show") == [entry]


async def test_a_device_that_never_finishes_announcing_is_offline_after_the_wait(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import registry as registry_module

    monkeypatch.setattr(registry_module, "LISTING_WAIT_S", 0.05)
    node = await pair_node(pairing, tmp_path)
    registry = NodeRegistry(pairing, nodes)
    client = _SlowListing([{"name": "notify.show", "_nox": {"group": "notify", "risk": "low"}}])
    session = _Session(pairing.get(node.node_id), client)
    connecting = asyncio.create_task(registry.on_connected(session))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    entry = next(e for e in registry.entries() if e.device_id == node.node_id)
    result = await registry.call(entry, "notify.show", {"text": "x"}, risk="low", timeout_s=5)
    assert result["error"] == "device_offline"
    assert not any(m == "tools/call" for m, _ in client.sent)
    client.answer.set()
    await connecting
