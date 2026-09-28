"""Manifest schema, loader and core-side egress authorization (ST-11-01 acceptance criteria 1-2,
7-8).

Every negative path here is a security boundary: namespace escape, secret-name escape, a
hard-prohibited tool and an egress endpoint the active profile does not allow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.plugins.manifest import (
    ManifestError,
    authorize_egress,
    check_egress,
    discover_manifest_paths,
    load_manifest,
    parse_manifest,
    split_endpoint,
    validate_secret_name,
    validate_tool_name,
)
from nox.security.model import Risk
from tests.unit.plugins.conftest import VALID_MANIFEST, make_profile, write_manifest


def test_valid_manifest_round_trips(plugins_dir: Path) -> None:
    path = write_manifest(plugins_dir, "demo")
    manifest = load_manifest(path)
    assert manifest.id == "demo"
    assert manifest.api_version == 1
    assert manifest.permissions[0].tool == "demo.ping"
    assert manifest.permissions[0].risk is Risk.READ
    assert manifest.events.emits == ["demo.pong"]
    assert manifest.matches_profile("companion") is True  # empty profiles = every profile


def test_profile_gate_is_manifest_driven() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "profiles": ["stream"]})
    assert manifest.matches_profile("stream") is True
    assert manifest.matches_profile("companion") is False


def test_a_network_plugin_is_an_integration_named_by_its_id_unless_it_says_otherwise() -> None:
    local = parse_manifest(VALID_MANIFEST)
    assert local.integration_id == "" and not local.reaches_network
    remote = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["api.example.com:443"]}})
    assert remote.integration_id == "demo" and remote.reaches_network
    loopback = parse_manifest(
        {**VALID_MANIFEST, "integration": "obs", "network": {"egress": ["127.0.0.1:4455"]}}
    )
    assert loopback.integration_id == "obs" and not loopback.reaches_network
    # A look-alike loopback name is a remote host like any other.
    rebinding = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["127.evil.example:80"]}})
    assert rebinding.reaches_network
    cloud = parse_manifest({**VALID_MANIFEST, "integration": "claude_code", "cloud": True})
    assert cloud.integration_id == "claude_code" and cloud.cloud


def test_a_malformed_integration_name_is_rejected() -> None:
    with pytest.raises(ManifestError, match="integration"):
        parse_manifest({**VALID_MANIFEST, "integration": "Home Assistant"})


def test_the_shipped_manifests_name_the_integrations_the_profiles_list() -> None:
    root = Path(__file__).resolve().parents[3] / "plugins"
    ids = {
        path.parent.name: load_manifest(path).integration_id
        for path in root.glob("*/manifest.yaml")
    }
    assert ids == {
        "clips": "",
        "coding": "claude_code",
        "creative": "",
        "echo": "",
        "home": "home_assistant",
        "obs": "obs",
        "rl": "",  # it has to run everywhere: it detects the game that switches the profile
        "telegram": "telegram",
        "twitch": "twitch",
    }


def test_tool_outside_namespace_is_rejected() -> None:
    with pytest.raises(ManifestError, match=r"outside the plugin's 'demo\.' namespace"):
        parse_manifest({**VALID_MANIFEST, "permissions": [{"tool": "obs.scene.switch"}]})


def test_hard_prohibited_tool_is_rejected() -> None:
    # A plugin whose id makes the prohibition look in-namespace still cannot request it.
    with pytest.raises(ManifestError, match="hard-prohibited"):
        parse_manifest(
            {
                **VALID_MANIFEST,
                "id": "stream",
                "entry": "nox_plugin_stream:create",
                "permissions": [{"tool": "stream.stop", "risk": "high"}],
                "events": {"emits": [], "listens": []},
            }
        )


def test_secret_outside_namespace_is_rejected() -> None:
    with pytest.raises(ManifestError, match=r"outside the plugin's 'nox/demo/' namespace"):
        parse_manifest({**VALID_MANIFEST, "secrets": ["nox/twitch/bot_oauth_token"]})


def test_malformed_secret_name_is_rejected() -> None:
    with pytest.raises(ManifestError, match="nox/<component>/<key>"):
        parse_manifest({**VALID_MANIFEST, "secrets": ["demo_password"]})


def test_unsupported_api_version_is_rejected() -> None:
    with pytest.raises(ManifestError, match="unsupported api_version"):
        parse_manifest({**VALID_MANIFEST, "api_version": 2})


def test_bad_entry_and_unknown_keys_are_rejected() -> None:
    with pytest.raises(ManifestError, match="package.module:callable"):
        parse_manifest({**VALID_MANIFEST, "entry": "nox_plugin_demo.create"})
    with pytest.raises(ManifestError, match="surprise"):
        parse_manifest({**VALID_MANIFEST, "surprise": True})


def test_manifest_id_must_match_its_directory(plugins_dir: Path) -> None:
    path = write_manifest(plugins_dir, "demo")
    (plugins_dir / "other").mkdir()
    (plugins_dir / "other" / "manifest.yaml").write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ManifestError, match="does not match its directory"):
        load_manifest(plugins_dir / "other" / "manifest.yaml")


def test_invalid_yaml_is_a_manifest_error(plugins_dir: Path) -> None:
    directory = plugins_dir / "broken"
    directory.mkdir()
    (directory / "manifest.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest(directory / "manifest.yaml")


def test_egress_entries_must_be_host_port() -> None:
    with pytest.raises(ManifestError, match="host:port"):
        parse_manifest({**VALID_MANIFEST, "network": {"egress": ["api.twitch.tv"]}})
    assert split_endpoint("api.twitch.tv:443") == ("api.twitch.tv", 443)


# ---- core-side egress authorization (ADR-013) ----------------------------------------------------


def test_egress_outside_the_active_profile_is_rejected() -> None:
    manifest = parse_manifest(
        {**VALID_MANIFEST, "network": {"egress": ["api.twitch.tv:443"]}},
    )
    stream = make_profile("stream", egress_allowlist=[], loopback_allowlist=["127.0.0.1:4455"])
    with pytest.raises(ManifestError, match="does not allow"):
        check_egress(manifest, stream)
    denied = authorize_egress(manifest, stream)
    assert [(d.entry, d.allowed) for d in denied] == [("api.twitch.tv:443", False)]


def test_egress_allowed_by_the_profile_list() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["api.twitch.tv:443"]}})
    profile = make_profile("stream", egress_allowlist=["*.twitch.tv:443"])
    results = check_egress(manifest, profile)
    assert results[0].allowed and results[0].reason == "*.twitch.tv:443"


def test_egress_falls_back_to_the_global_allowlist() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["api.twitch.tv:443"]}})
    profile = make_profile("companion")  # empty list + cloud_allowed -> inherit global
    assert check_egress(manifest, profile, global_allowlist=["api.twitch.tv:443"])[0].allowed
    with pytest.raises(ManifestError):
        check_egress(manifest, profile, global_allowlist=[])


def test_loopback_egress_needs_the_loopback_allowlist() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["127.0.0.1:4455"]}})
    stream = make_profile("stream", loopback_allowlist=["127.0.0.1:4455"])
    assert check_egress(manifest, stream)[0].allowed
    with pytest.raises(ManifestError, match="does not allow"):
        check_egress(manifest, make_profile("companion"))


def test_profile_entry_none_blocks_everything() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "network": {"egress": ["api.twitch.tv:443"]}})
    offline = make_profile("offline", egress_allowlist=["none"], cloud_allowed=False)
    with pytest.raises(ManifestError, match="forbids egress"):
        check_egress(manifest, offline)


# ---- helpers used by the worker-side API ---------------------------------------------------------


def test_validate_tool_and_secret_names() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "secrets": ["nox/demo/token"]})
    assert validate_tool_name(manifest, "demo.ping").risk is Risk.READ
    with pytest.raises(ManifestError, match="not declared in the manifest"):
        validate_tool_name(manifest, "demo.other")
    with pytest.raises(ManifestError, match=r"outside the 'demo\.' namespace"):
        validate_tool_name(manifest, "twitch.chat.send")
    assert validate_secret_name(manifest, "nox/demo/token") == "nox/demo/token"
    with pytest.raises(ManifestError, match="not declared"):
        validate_secret_name(manifest, "nox/demo/other")


@pytest.mark.parametrize(
    "event",
    [
        "privacy.capture_changed",
        "security.kill_switch",
        "system.started",
        "sup.kill",
        "worker.registered",
        "ipc.client_connected",
        "plugin.started",
        "voice.kill_phrase",
        "memory.written",
    ],
)
def test_a_manifest_cannot_emit_into_a_namespace_the_core_reserves(event: str) -> None:
    with pytest.raises(ManifestError, match="only the core may publish"):
        parse_manifest({**VALID_MANIFEST, "events": {"emits": [event], "listens": []}})


def test_emitted_events_must_be_exact_names_not_patterns() -> None:
    with pytest.raises(ManifestError, match="exact name"):
        parse_manifest({**VALID_MANIFEST, "events": {"emits": ["demo.*"], "listens": []}})


def test_events_outside_the_own_namespace_stay_possible_when_declared() -> None:
    manifest = parse_manifest(
        {**VALID_MANIFEST, "events": {"emits": ["demo.pong", "stream.started"], "listens": []}}
    )
    assert manifest.events.emits == ["demo.pong", "stream.started"]


def test_required_tools_are_exact_and_never_hard_prohibited() -> None:
    manifest = parse_manifest({**VALID_MANIFEST, "requires": {"tools": ["memory.search"]}})
    assert manifest.requires.tools == ["memory.search"]
    with pytest.raises(ManifestError, match="exact dotted"):
        parse_manifest({**VALID_MANIFEST, "requires": {"tools": ["memory.*"]}})
    with pytest.raises(ManifestError, match="hard-prohibited"):
        parse_manifest({**VALID_MANIFEST, "requires": {"tools": ["game.input.send"]}})


def test_a_preflight_must_be_a_declared_read_tool_of_the_same_plugin() -> None:
    permissions = [
        {"tool": "demo.act", "risk": "medium", "preflight": "demo.check"},
        {"tool": "demo.check", "risk": "read"},
    ]
    manifest = parse_manifest({**VALID_MANIFEST, "permissions": permissions})
    assert manifest.permissions[0].preflight == "demo.check"
    with pytest.raises(ManifestError, match="not a tool this manifest declares"):
        parse_manifest(
            {
                **VALID_MANIFEST,
                "permissions": [{"tool": "demo.act", "risk": "medium", "preflight": "x.check"}],
            }
        )
    with pytest.raises(ManifestError, match="read-risk"):
        parse_manifest(
            {
                **VALID_MANIFEST,
                "permissions": [
                    {"tool": "demo.act", "risk": "medium", "preflight": "demo.check"},
                    {"tool": "demo.check", "risk": "low"},
                ],
            }
        )


def test_discovery_lists_only_directories_with_a_manifest(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    write_manifest(plugins_dir, "other", entry="nox_plugin_other:create")
    (plugins_dir / "not_a_plugin").mkdir()
    assert [p.parent.name for p in discover_manifest_paths(plugins_dir)] == ["demo", "other"]
    assert discover_manifest_paths(plugins_dir / "missing") == []
