"""`plugins/home/manifest.yaml` validates on its own and against the real `companion` profile.

The tool table below is the shipped surface of the feature. It is asserted by exact equality on
purpose: adding a tool to the manifest without a reviewer noticing is exactly the change that
could put a door lock within reach of the AI layer.
"""

from __future__ import annotations

from nox.app import PROFILES_DIR, REPO_ROOT
from nox.home.boundary import FORBIDDEN_DOMAINS
from nox.plugins.manifest import check_egress, load_manifest
from nox.security.model import Risk
from nox.security.profiles import load_profile

HOME_PLUGIN_DIR = REPO_ROOT / "plugins" / "home"

EXPECTED_TOOLS = {
    "home.status.read": Risk.READ,
    "home.list": Risk.READ,
    "home.state": Risk.READ,
    "home.light": Risk.MEDIUM,
    "home.switch": Risk.MEDIUM,
    "home.scene": Risk.MEDIUM,
    "home.media": Risk.MEDIUM,
    "home.climate": Risk.MEDIUM,
    "home.cover": Risk.MEDIUM,
    "home.script": Risk.HIGH,
    "home.automation.trigger": Risk.HIGH,
}


def test_home_manifest_loads_and_declares_exactly_the_expected_tools() -> None:
    manifest = load_manifest(HOME_PLUGIN_DIR)
    assert manifest.id == "home"
    assert manifest.entry == "nox_plugin_home:create"
    assert manifest.matches_profile("companion") is True
    assert manifest.matches_profile("stream") is False
    assert {p.tool: p.risk for p in manifest.permissions} == EXPECTED_TOOLS


def test_home_manifest_has_no_tool_for_a_domain_that_secures_the_building() -> None:
    """The hard boundary is visible in the manifest, not only in the code that enforces it."""
    manifest = load_manifest(HOME_PLUGIN_DIR)
    tools = {p.tool for p in manifest.permissions}
    for domain in FORBIDDEN_DOMAINS:
        assert f"home.{domain}" not in tools
    assert "home.lock" not in tools
    assert "home.unlock" not in tools
    assert "home.alarm" not in tools
    assert "home.garage" not in tools
    # No generic escape hatch either: a "call any service" tool would defeat the whole boundary.
    assert "home.service.call" not in tools
    assert "home.call" not in tools


def test_home_manifest_declares_only_the_access_token_secret() -> None:
    manifest = load_manifest(HOME_PLUGIN_DIR)
    assert manifest.secrets == ["nox/home/access_token"]


def test_home_manifest_subscribes_to_state_changes_only() -> None:
    """A subscription is what leaves Home Assistant unasked; there is exactly one."""
    manifest = load_manifest(HOME_PLUGIN_DIR)
    assert manifest.config["subscribe_event_types"] == ["state_changed"]
    assert manifest.events.emits == ["home.connected", "home.disconnected", "home.state_changed"]
    assert manifest.events.listens == ["security.panic"]


def test_home_manifest_egress_is_authorized_by_the_real_companion_profile() -> None:
    manifest = load_manifest(HOME_PLUGIN_DIR)
    companion = load_profile(PROFILES_DIR / "companion.yaml")
    results = check_egress(manifest, companion)
    assert [(r.entry, r.allowed) for r in results] == [
        ("127.0.0.1:8123", True),
        ("homeassistant.local:8123", True),
    ]
