"""`nox.node.policy`: the core's own tool -> group mapping, what a listing records, and that a
narrowed group stays out of what the core routes - whatever the node says."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nox.node.manifest import GROUPS
from nox.node.pairing import NodePairing
from nox.node.policy import NodePolicy, defaults_view, family_tools, group_of
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
    assert not policy.enabled_on_device(node.node_id, "shell")


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
