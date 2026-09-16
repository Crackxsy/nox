"""`plugins/clips/manifest.yaml` validates and declares no tool surface (ST-15-01 acceptance
criteria: no tool touching the source recording exists - here, no tool exists at all)."""

from __future__ import annotations

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.plugins.manifest import check_egress, load_manifest
from nox.security.profiles import load_profile

CLIPS_PLUGIN_DIR = REPO_ROOT / "plugins" / "clips"


def test_clips_manifest_loads_and_declares_no_tools() -> None:
    manifest = load_manifest(CLIPS_PLUGIN_DIR)
    assert manifest.id == "clips"
    assert manifest.entry == "nox_plugin_clips:create"
    assert manifest.permissions == []
    assert manifest.matches_profile("stream") is True
    assert manifest.matches_profile("companion") is True


def test_clips_manifest_events() -> None:
    manifest = load_manifest(CLIPS_PLUGIN_DIR)
    assert manifest.events.emits == ["clip.requested"]
    assert set(manifest.events.listens) == {
        "rl.event",
        "twitch.chat_mood_changed",
        "twitch.command_invoked",
        "stream.started",
        "stream.ended",
        "stream.mode_changed",
        "security.kill_switch",
        "security.panic",
    }


def test_clips_manifest_has_no_secrets_and_no_egress() -> None:
    manifest = load_manifest(CLIPS_PLUGIN_DIR)
    assert manifest.secrets == []
    assert manifest.network.egress == []
    stream = load_profile(PROFILES_DIR / "stream.yaml")
    assert check_egress(manifest, stream) == []
