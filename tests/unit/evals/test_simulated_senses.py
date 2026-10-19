"""The benchmark world simulates the browser, screen and camera - and only then approves them.

Confirmations stay deny-by-default: a simulated family is approvable only when a task lists the
tool and the world has really swapped its backend; without the swap the same dialog is denied.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kann_alles.confirm import decide
from kann_alles.env import SENSES_ROOT, SITE_URL, World
from nox.core.state import PrivacyMode
from nox.hands.browser import BrowserSession, BrowserSettings
from nox.hands.install import HandsRuntime
from nox.hands.senses import Senses
from nox.sandbox.netguard import NetGuard
from nox.security.model import Decision


def _decide(tmp_path: Path, **overrides: Any) -> Decision:
    args: dict[str, Any] = {
        "tool": "screen",
        "action": "capture",
        "risk": "medium",
        "target": "screen",
        "confirm_tools": [r"screen\.capture"],
        "forbidden_tools": [],
        "sandbox": tmp_path / "sandbox",
        "root": tmp_path,
    }
    args.update(overrides)
    return decide(**args).decision


def test_a_listed_capture_is_denied_unless_the_world_simulates_it(tmp_path: Path) -> None:
    assert _decide(tmp_path) is Decision.DENY
    assert _decide(tmp_path, simulated=frozenset({"screen"})) is Decision.ALLOW
    assert _decide(tmp_path, simulated=frozenset({"camera"})) is Decision.DENY


def test_simulation_never_approves_an_unlisted_or_forbidden_or_high_tool(tmp_path: Path) -> None:
    sim = frozenset({"screen", "camera", "browser"})
    assert _decide(tmp_path, simulated=sim, confirm_tools=[]) is Decision.DENY
    assert _decide(tmp_path, simulated=sim, forbidden_tools=[r"screen\..*"]) is Decision.DENY
    assert _decide(tmp_path, simulated=sim, risk="high") is Decision.DENY
    browser = {"tool": "browser", "action": "submit", "target": "origin:http://x"}
    assert (
        _decide(tmp_path, simulated=sim, confirm_tools=[r"browser\.submit"], **browser)
        is Decision.ALLOW
    )


class _Privacy:
    mode = PrivacyMode.BALANCED
    active_zone = None

    def __init__(self) -> None:
        self.camera = False

    def allows_capture(self, kind: str) -> bool:
        return kind != "camera" or self.camera

    def allows_cloud(self) -> bool:
        return True

    def allows_screenshot_to_cloud(self) -> bool:
        return False

    async def set_capture(self, *, camera: bool, by: str) -> None:
        assert by == "eval"
        self.camera = camera


async def test_the_world_swaps_every_backend_before_it_approves_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    privacy = _Privacy()

    async def real_device() -> bytes:
        raise AssertionError("the world must never touch a real screen or camera")

    senses = Senses(
        captures_root=tmp_path / "captures",
        privacy=privacy,  # type: ignore[arg-type]
        screen=real_device,
        camera=real_device,
    )
    browser = BrowserSession(lambda: BrowserSettings(), NetGuard())
    core = SimpleNamespace(
        extensions={"hands": HandsRuntime(browser=browser, senses=senses)},
        security=SimpleNamespace(privacy=privacy),
    )
    world = World(tmp_path / "world")
    world.web.start()
    try:
        await world.simulate(core)
    finally:
        world.web.stop()

    assert world.simulated == frozenset({"browser", "screen", "camera"})
    assert privacy.camera is True
    shot = await senses.capture_screen()
    assert Path(shot["path"]).read_bytes() == (SENSES_ROOT / "screen.png").read_bytes()  # noqa: ASYNC240
    described = await senses.describe(shot["path"], "Was steht im Kalender?")
    assert described["processed"] == "local" and "Zahnarzt" in described["description"]
    photo = await senses.snapshot_camera()
    assert "Kaffeetasse" in (await senses.describe(photo["path"], ""))["description"]
    assert senses.cloud_vision is None
    # The browser resolves only the fake web; the rest of the internet does not exist here.
    resolver = browser._resolver  # noqa: SLF001
    host = SITE_URL.removeprefix("http://")
    assert resolver(host, 80) == [ipaddress.ip_address("93.184.216.34")]
    with pytest.raises(OSError):
        resolver("example.com", 443)
    assert resolver("192.168.0.1", 80) == [ipaddress.ip_address("192.168.0.1")]


def test_site_placeholder_renders(tmp_path: Path) -> None:
    world = World(tmp_path / "world")
    assert world.render("{site}/fahrplan.html") == f"{SITE_URL}/fahrplan.html"
