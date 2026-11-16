"""The catalogue of a node's senses and input tools: every tool has a group, everything but
music is off by default, a tool whose library or operating system is missing is not
offered, and enabling computer use takes a second yes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.node.gate import NodeGate
from nox.node.manifest import NodeManifest
from nox.node.policy import relevant_groups
from nox.node.registry import ROUTABLE, NodeRegistry
from nox.node.tools import NodeRefusal
from nox.node.world import PlatformMedia, world_tools
from tests.unit.node.support import registry_entry
from tests.unit.node.support_sessions import LAPTOP, SENSE_GROUPS, device_world, world_gate

# ---- the catalogue and its groups ---------------------------------------------------------------


def test_every_c6_tool_has_a_group_and_is_off_by_default_except_music() -> None:
    world, _ = device_world()
    tools = world_tools(world)
    assert set(tools) <= ROUTABLE
    assert {t.group for t in tools.values()} == set(SENSE_GROUPS)
    defaults = NodeManifest()
    for group in SENSE_GROUPS:
        # Spec C §1.4: music is on by default (low risk); everything else only on the device.
        assert defaults.groups[group] is (group == "media"), group
    gate = NodeGate(tools, defaults, None)
    assert {t.name for t in gate.offered()} == {"media.control"}
    # Captures are high on a node: asked on the device, every time.
    assert tools["screen.capture"].risk == tools["camera.snapshot"].risk == "high"
    assert tools["computer.session_start"].risk == "high"
    assert "browser.screenshot" not in tools  # it would keep a file on the device


def test_the_dashboard_offers_the_new_groups() -> None:
    assert set(SENSE_GROUPS) <= relevant_groups()


def test_narrowing_on_the_core_hides_them(tmp_path: Path) -> None:
    entry = registry_entry(
        LAPTOP, "Laptop", tools={"screen.capture": "high", "media.control": "low"}
    )
    entry.narrowed = frozenset({"screen"})
    assert not entry.offers("screen.capture") and entry.offers("media.control")
    world, _ = device_world()
    gate, _signer = world_gate(world, tmp_path)
    gate.narrow(["computer", "camera"])
    offered = {t.name for t in gate.offered()}
    assert not any(n.startswith(("computer.", "camera.")) for n in offered)
    assert "screen.capture" in offered


async def test_desktop_and_computer_are_windows_only() -> None:
    world, seen = device_world(windows=False)
    # Off Windows they are not offered at all (no status claims them) ...
    assert not any(n.startswith(("desktop.", "computer.")) for n in world_tools(world))
    # ... and a handler made on Windows refuses once the device is not Windows (defence in depth).
    world.windows = True
    tools = world_tools(world)
    world.windows = False
    calls = {
        "computer.session_start": {"purpose": "Fenster ordnen"},
        "desktop.windows": {},
        "desktop.type_text": {"title": "x", "text": "y"},
    }
    for name, raw in calls.items():
        tool = tools[name]
        args = tool.args.model_validate(raw)
        with pytest.raises(NodeRefusal) as refused:
            await tool.handler(args)
        assert refused.value.reason == "windows_only"
    assert seen["sender"].events == []


def test_enabling_computer_needs_windows_and_a_second_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import cli
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "node")
    monkeypatch.setattr(cli, "computer_supported", lambda: False)
    with pytest.raises(cli.NodeCliError, match="not available on this device"):
        cli.allow(
            home, "computer", True, say=print, interactive=lambda: True, confirm=lambda _p: True
        )
    monkeypatch.setattr(cli, "computer_supported", lambda: True)
    asked: list[str] = []

    def first_only(prompt: str) -> bool:
        asked.append(prompt)
        return len(asked) == 1

    with pytest.raises(cli.NodeCliError, match="Not allowed"):
        cli.allow(home, "computer", True, say=print, interactive=lambda: True, confirm=first_only)
    assert len(asked) == 2 and "mouse" in asked[1]


def test_the_routes_keep_registry_entries_consistent() -> None:
    accepted = NodeRegistry._accept_tools([{"name": n, "_nox": {"risk": "low"}} for n in ROUTABLE])
    assert set(accepted) == ROUTABLE


def test_building_the_tools_touches_no_window_api() -> None:
    """Off Windows the probe cannot even be made: building the node's tools must not try."""
    world, _ = device_world(windows=False)

    def no_windows() -> Any:
        raise RuntimeError("RealWindowProbe requires Windows (ctypes.windll)")

    world.probe = no_windows
    tools = world_tools(world)
    assert "desktop.windows" not in tools and "media.control" in tools


def test_a_tool_whose_library_or_os_is_missing_is_not_offered() -> None:
    """Review panel round 1 (H1): the node never lists what cannot work here, so the core's
    status and the dashboard never claim it."""
    from nox.node.world import withheld

    world, _ = device_world()
    world.screen = world.camera = None
    world.has_module = lambda module: module not in ("mss", "cv2", "playwright")
    tools = world_tools(world)
    assert not {"screen.capture", "camera.snapshot"} & set(tools)
    assert not any(name.startswith("browser.") for name in tools)
    assert "media.control" in tools and "desktop.windows" in tools
    assert "senses" in withheld(world)["screen.capture"]
    assert "browser" in withheld(world)["browser."]
    world.has_module = lambda _module: True
    assert {"screen.capture", "camera.snapshot", "browser.open"} <= set(world_tools(world))
    world.media = PlatformMedia(platform="darwin")
    assert "media.control" not in world_tools(world)
