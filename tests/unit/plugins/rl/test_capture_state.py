"""The Rocket League plugin's screen-capture gate starts from the core's current capture state.

The gate used to start open and learn about `privacy.capture.screen: false`, a privacy zone or a
panic only from the next `privacy.capture_changed` event - so a plugin that started while one of
those was already in force grabbed HUD frames until something changed. The worker now hands the
plugin the state from `plugin.register`, and `start()` applies it before any loop runs.
"""

from __future__ import annotations

from pathlib import Path

from nox_plugin_rl.plugin import RlPlugin

from nox.core.state import PrivacyMode
from nox.plugins.api import PluginApi, PrivacyView
from nox.plugins.manifest import MANIFEST_FILE, load_manifest
from tests.unit.plugins.test_api import FakeClient

MANIFEST = Path(__file__).resolve().parents[4] / "plugins" / "rl" / MANIFEST_FILE


def _plugin(tmp_path: Path, view: PrivacyView) -> RlPlugin:
    manifest = load_manifest(MANIFEST, expected_id="rl")
    config = {
        **manifest.config,
        "replay_folder": str(tmp_path / "replays"),
        "calibration_state_path": str(tmp_path / "calibration.json"),
        "detection": {"process_names": ["no-such-game.exe"], "poll_interval_s": 60},
    }
    (tmp_path / "replays").mkdir()
    api = PluginApi(manifest=manifest, client=FakeClient(), config=config, privacy=view)
    return RlPlugin(api)


async def test_screen_capture_closed_before_start_stays_closed(tmp_path: Path) -> None:
    view = PrivacyView(PrivacyMode.BALANCED, capture={"screen": False})
    plugin = _plugin(tmp_path, view)

    await plugin.start()
    try:
        assert plugin._capture_allowed is False
    finally:
        await plugin.stop()


async def test_screen_capture_allowed_by_the_core_opens_the_gate(tmp_path: Path) -> None:
    view = PrivacyView(PrivacyMode.BALANCED, capture={"screen": True})
    plugin = _plugin(tmp_path, view)

    await plugin.start()
    try:
        assert plugin._capture_allowed is True
    finally:
        await plugin.stop()


async def test_no_capture_before_the_core_has_answered(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, PrivacyView())

    await plugin.start()
    try:
        assert plugin._capture_allowed is False
    finally:
        await plugin.stop()
