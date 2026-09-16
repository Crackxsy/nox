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


def test_event_namespaces_cover_id_and_declared_emit_prefixes() -> None:
    manifest = parse_manifest(
        {**VALID_MANIFEST, "events": {"emits": ["demo.pong", "stream.started"], "listens": []}}
    )
    assert manifest.event_namespaces() == frozenset({"demo", "stream"})


def test_discovery_lists_only_directories_with_a_manifest(plugins_dir: Path) -> None:
    write_manifest(plugins_dir, "demo")
    write_manifest(plugins_dir, "other", entry="nox_plugin_other:create")
    (plugins_dir / "not_a_plugin").mkdir()
    assert [p.parent.name for p in discover_manifest_paths(plugins_dir)] == ["demo", "other"]
    assert discover_manifest_paths(plugins_dir / "missing") == []
