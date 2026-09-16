"""`plugins/rl/manifest.yaml` validates on its own and against the real `rocket_league` profile
(ST-12-01 AC) - mirrors `tests/unit/plugins/obs/test_manifest.py`."""

from __future__ import annotations

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.plugins.manifest import load_manifest
from nox.security.model import Risk
from nox.security.profiles import load_profile

RL_PLUGIN_DIR = REPO_ROOT / "plugins" / "rl"


def test_rl_manifest_loads_and_declares_exactly_the_expected_tools() -> None:
    manifest = load_manifest(RL_PLUGIN_DIR)
    assert manifest.id == "rl"
    assert manifest.entry == "nox_plugin_rl:create"
    # `profiles: []` - matches every active profile so the detector can run from boot and drive
    # the switch *into* `rocket_league` (see manifest.yaml's comment; ST-11-01's
    # `PluginManager._apply_enablement` would deadlock startup otherwise).
    assert manifest.matches_profile("rocket_league") is True
    assert manifest.matches_profile("companion") is True
    tools = {p.tool: p.risk for p in manifest.permissions}
    assert tools == {
        "rl.status.read": Risk.READ,
        "rl.replay.list": Risk.READ,
        "rl.replay.summary": Risk.READ,
        "rl.calibrate": Risk.MEDIUM,
        "rl.vision.status.read": Risk.READ,
        "rl.vision.enable": Risk.MEDIUM,
    }
    # FR-10.1: these verbs do not exist here at all, on top of being hard-prohibited.
    for forbidden in ("rl.input.", "rl.memory.", "rl.inject."):
        assert not any(t.startswith(forbidden) for t in tools)


def test_rl_manifest_events_match_events_py() -> None:
    manifest = load_manifest(RL_PLUGIN_DIR)
    assert set(manifest.events.emits) == {
        "game.detected",
        "game.ended",
        "rl.match_started",
        "rl.match_ended",
        "rl.event",
        "rl.replay_parsed",
        "rl.callout",
        "rl.vision.detections",
        "rl.vision.disabled",
    }
    assert set(manifest.events.listens) == {
        "system.mode_changed",
        "security.kill_switch",
        "security.panic",
        "privacy.capture_changed",
        "privacy.zone_changed",
    }


def test_rl_manifest_has_no_secrets_and_no_egress() -> None:
    manifest = load_manifest(RL_PLUGIN_DIR)
    assert manifest.secrets == []
    assert manifest.network.egress == []


def test_rocket_league_profile_matches_spec_6_5() -> None:
    profile = load_profile(PROFILES_DIR / "rocket_league.yaml")
    assert profile.id == "rocket_league"
    assert profile.cloud_allowed is False
    assert profile.egress_allowlist == []
    assert profile.loopback_allowlist == []
    assert profile.integrations_allowed == ["rl"]
    rule_ids = {r.id for r in profile.rules}
    assert "rocket_league.rl.read" in rule_ids
    assert "rocket_league.rl.calibrate" in rule_ids
    assert "rocket_league.rl.vision_enable" in rule_ids
