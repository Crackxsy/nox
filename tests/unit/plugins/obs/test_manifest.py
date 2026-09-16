"""`plugins/obs/manifest.yaml` validates on its own and against the real `stream` profile
(`config/profiles/stream.yaml`) - same approach as `tests/unit/plugins/test_manifest.py`."""

from __future__ import annotations

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.plugins.manifest import check_egress, load_manifest
from nox.security.model import Risk
from nox.security.profiles import load_profile

OBS_PLUGIN_DIR = REPO_ROOT / "plugins" / "obs"


def test_obs_manifest_loads_and_declares_the_expected_tools() -> None:
    manifest = load_manifest(OBS_PLUGIN_DIR)
    assert manifest.id == "obs"
    assert manifest.entry == "nox_plugin_obs:create"
    assert manifest.matches_profile("stream") is True
    assert manifest.matches_profile("companion") is False
    tools = {p.tool: p.risk for p in manifest.permissions}
    assert tools == {
        "obs.status.read": Risk.READ,
        "obs.scenes.list": Risk.READ,
        "obs.scene.switch": Risk.MEDIUM,
        "obs.privacy_scene.activate": Risk.MEDIUM,
        "obs.preflight.check": Risk.READ,
        "obs.replay_buffer.save": Risk.MEDIUM,
        "obs.replay_buffer.status.read": Risk.READ,
    }
    # Never present, on top of being hard-prohibited/profile-denied (Spec v0.2 §5/§6.1).
    assert "obs.scene.delete" not in tools
    assert "obs.scene.rename" not in tools
    assert "obs.source.delete" not in tools
    assert not any(t.startswith("stream.stop") or "recording.delete" in t for t in tools)


def test_obs_manifest_events_match_events_py() -> None:
    manifest = load_manifest(OBS_PLUGIN_DIR)
    assert set(manifest.events.emits) == {
        "obs.connected",
        "obs.disconnected",
        "obs.scene_changed",
        "stream.started",
        "stream.ended",
        "obs.recording_changed",
    }
    assert manifest.events.listens == ["security.panic"]


def test_obs_manifest_secret_is_declared() -> None:
    manifest = load_manifest(OBS_PLUGIN_DIR)
    assert manifest.secrets == ["nox/obs/websocket_password"]


def test_obs_manifest_egress_is_authorized_by_the_real_stream_profile() -> None:
    manifest = load_manifest(OBS_PLUGIN_DIR)
    stream = load_profile(PROFILES_DIR / "stream.yaml")
    results = check_egress(manifest, stream)
    assert [(r.entry, r.allowed) for r in results] == [("127.0.0.1:4455", True)]
