"""`creative` plugin, end to end (Spec v0.7 Creative Apps, EPIC-16): a real `nox.core.bus.
AsyncEventBus` plus the core-side services from `nox.creative.install.install` react to the real
`nox_plugin_creative.plugin.CreativePlugin` wired to a `FakeClient` bridged onto that bus.

This does NOT spawn a real `NoxCore`/worker subprocess like `tests/integration/test_obs_plugin.py`
does: `nox.creative.install.install` is not wired into `NoxCore.start()` (this task may not edit
`src/nox/app.py` - see the coordination note in the implementing agent's report), so a real
`NoxCore` run would never call it either. Bridging `FakeClient` onto a real bus instead exercises
the full plugin <-> core-service contract (event names, payload shapes, the hysteresis timer under
real asyncio scheduling) without needing that wiring.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import Event
from nox.core.state import Mode, PrivacyMode
from nox.creative.install import install

REPO_ROOT = Path(__file__).resolve().parents[2]
_CREATIVE_SRC = REPO_ROOT / "plugins" / "creative" / "src"
if str(_CREATIVE_SRC) not in sys.path:
    sys.path.insert(0, str(_CREATIVE_SRC))

from nox_plugin_creative import CreativePlugin, create  # noqa: E402

from nox.ipc.protocol import Envelope, Kind, Source  # noqa: E402
from nox.plugins.api import PluginApi  # noqa: E402
from nox.plugins.manifest import parse_manifest  # noqa: E402
from tests.unit.plugins.creative.conftest import CREATIVE_MANIFEST  # noqa: E402


class _FakeProfile:
    def __init__(self, profile_id: str) -> None:
        self.id = profile_id


class _FakeEngine:
    def __init__(self, profile_id: str = "companion") -> None:
        self._profile = _FakeProfile(profile_id)

    def active_profile(self) -> _FakeProfile:
        return self._profile


class _FakePrivacy:
    def __init__(self, mode: PrivacyMode = PrivacyMode.FULL) -> None:
        self.mode = mode

    def zone_active(self, window_title: str = "", process_name: str = "") -> bool:
        return False

    def allows_capture(self, kind: str) -> bool:
        return True


class _FakeSecurity:
    def __init__(self, profile_id: str = "companion") -> None:
        self.engine = _FakeEngine(profile_id)
        self.privacy = _FakePrivacy()


class _FakeState:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            "assistant.mode": "companion",
            "assistant.mode_locked": False,
        }

    def get(self, path: str) -> Any:
        return self.values.get(path)

    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        self.values[path] = value
        return 1


class _FakePaths:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir


class _FakeConfig:
    def __init__(self, runtime_dir: Path) -> None:
        self.paths = _FakePaths(runtime_dir)


class _Core:
    """The minimal `core` surface `nox.creative.install.install` needs, backed by a real bus."""

    def __init__(self, tmp_path: Path, *, profile_id: str = "companion") -> None:
        self.bus = AsyncEventBus()
        self.state = _FakeState()
        self.security = _FakeSecurity(profile_id)
        self.config = _FakeConfig(tmp_path)


class BridgedClient:
    """`PluginClient` that also publishes every emitted event onto a real core bus, and forwards
    every bus event back down to whatever this plugin `.on()`-registered - the two directions the
    real `IpcHub` bridges in production (`send_event` -> hub -> `bus.publish`; `bus.subscribe("**")`
    -> matching client subscriptions), simplified for an in-process test."""

    def __init__(self, bus: AsyncEventBus) -> None:
        self._bus = bus
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}
        self._bus.subscribe("**", self._on_bus_event)

    async def request(
        self, name: str, payload: Any = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return {}

    async def send_event(self, name: str, payload: Any = None, *, corr: str | None = None) -> None:
        self.events.append((name, dict(payload or {})))
        await self._bus.publish(Event(name=name, payload=dict(payload or {})))

    def on(self, name_glob: str, handler: Any) -> Any:
        self.handlers.setdefault(name_glob, []).append(handler)

        def _unsub() -> None:
            self.handlers[name_glob].remove(handler)

        return _unsub

    async def _on_bus_event(self, ev: Event) -> None:
        await self.fire(ev.name, dict(ev.payload))

    async def fire(self, name: str, payload: dict[str, Any] | None = None) -> None:
        env = Envelope(
            kind=Kind.EVENT,
            name=name,
            src=Source(role="core", id="core"),
            payload=dict(payload or {}),
        )
        for handler in list(self.handlers.get(name, [])):
            result = handler(env)
            if result is not None:
                await result


def _make_plugin(client: BridgedClient, *, hysteresis_s: float = 0.05) -> CreativePlugin:
    config = {**CREATIVE_MANIFEST["config"], "hysteresis_s": hysteresis_s}
    manifest = parse_manifest({**CREATIVE_MANIFEST, "config": config})
    api = PluginApi(manifest=manifest, client=client)
    plugin = create(api)
    return plugin


@pytest.mark.timeout(30)
class TestDetectionToModeSwitch:
    async def test_sustained_detection_switches_mode_and_fires_once(self, tmp_path: Path) -> None:
        core = _Core(tmp_path)
        install(core)
        client = BridgedClient(core.bus)
        plugin = _make_plugin(client)
        await plugin.start()

        await client.fire(
            "sensor.foreground_changed", {"process": "blender.exe", "title": "x.blend"}
        )
        await asyncio.sleep(0.15)  # past the 0.05s test hysteresis window

        detected = [e for e in client.events if e[0] == "creative.app_detected"]
        assert len(detected) == 1
        assert detected[0][1] == {"app": "blender", "window_title": "x.blend"}
        assert core.state.get("assistant.mode") == Mode.CREATIVE.value

    async def test_short_alt_tab_does_not_flap_or_switch_mode_twice(self, tmp_path: Path) -> None:
        core = _Core(tmp_path)
        install(core)
        client = BridgedClient(core.bus)
        plugin = _make_plugin(client, hysteresis_s=0.2)
        await plugin.start()

        await client.fire("sensor.foreground_changed", {"process": "blender.exe", "title": "x"})
        await asyncio.sleep(0.25)
        assert core.state.get("assistant.mode") == Mode.CREATIVE.value

        # Alt-tab away briefly, then back - well within the window.
        await client.fire("sensor.foreground_changed", {"process": "explorer.exe", "title": "y"})
        await asyncio.sleep(0.05)
        await client.fire("sensor.foreground_changed", {"process": "blender.exe", "title": "x"})
        await asyncio.sleep(0.25)

        detected = [e for e in client.events if e[0] == "creative.app_detected"]
        left = [e for e in client.events if e[0] == "creative.app_left"]
        assert len(detected) == 1
        assert len(left) == 0
        assert core.state.get("assistant.mode") == Mode.CREATIVE.value

    async def test_leaving_reverts_mode(self, tmp_path: Path) -> None:
        core = _Core(tmp_path)
        install(core)
        client = BridgedClient(core.bus)
        plugin = _make_plugin(client)
        await plugin.start()
        await client.fire("sensor.foreground_changed", {"process": "blender.exe", "title": "x"})
        await asyncio.sleep(0.15)
        assert core.state.get("assistant.mode") == Mode.CREATIVE.value

        await client.fire("sensor.foreground_changed", {"process": "explorer.exe", "title": "y"})
        await asyncio.sleep(0.15)

        assert core.state.get("assistant.mode") == Mode.COMPANION.value
        assert any(e[0] == "creative.app_left" for e in client.events)


@pytest.mark.timeout(30)
class TestScreenshotFlowEndToEnd:
    async def test_refused_in_private_mode(self, tmp_path: Path) -> None:
        core = _Core(tmp_path)
        core.security.privacy.mode = PrivacyMode.PRIVATE
        install(core)
        client = BridgedClient(core.bus)
        plugin = _make_plugin(client)
        await plugin.start()
        plugin.detector.current = "blender"
        plugin._last_title = "x.blend"

        from nox_plugin_creative.plugin import EmptyInput

        result = await plugin.screenshot_analyze(EmptyInput())

        assert result["status"] == "refused"
        assert "private" in result["reason"].lower()

    async def test_unavailable_without_mss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nox.creative.screenshot as screenshot_mod

        monkeypatch.setattr(screenshot_mod, "mss_available", lambda: False)
        core = _Core(tmp_path)  # defaults to FULL, no zone, no Work profile
        install(core)
        client = BridgedClient(core.bus)
        plugin = _make_plugin(client)
        await plugin.start()
        plugin.detector.current = "blender"
        plugin._last_title = "x.blend"

        from nox_plugin_creative.plugin import EmptyInput

        result = await plugin.screenshot_analyze(EmptyInput())

        assert result["status"] == "unavailable"
        assert "mss" in result["reason"]
