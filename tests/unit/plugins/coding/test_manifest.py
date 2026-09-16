"""Manifest validation: namespace, declared tool risk, and profile gating - `coding` never matches
any profile other than `coding` (ST-14-02 acceptance: refused before any subprocess exists; here
verified at the manifest level, the structural guarantee the plugin manager itself enforces before
ever spawning a worker)."""

from __future__ import annotations

from pathlib import Path

from nox.plugins.manifest import load_manifest

from .conftest import make_manifest

MANIFEST_PATH = Path(__file__).resolve().parents[4] / "plugins" / "coding" / "manifest.yaml"


def test_shipped_manifest_loads_and_validates() -> None:
    manifest = load_manifest(MANIFEST_PATH, expected_id="coding")
    assert manifest.id == "coding"
    assert manifest.entry == "nox_plugin_coding:create"
    assert manifest.profiles == ["coding"]
    assert sorted(p.tool for p in manifest.permissions) == [
        "coding.review.request",
        "coding.session.start",
        "coding.session.status.read",
        "coding.session.stop",
    ]
    assert manifest.events.emits == [
        "coding.session_started",
        "coding.session_progress",
        "coding.session_ended",
        "coding.session_failed",
    ]
    assert manifest.events.listens == ["security.kill_switch"]
    assert manifest.network.egress == []


def test_manifest_matches_only_the_coding_profile() -> None:
    manifest = load_manifest(MANIFEST_PATH, expected_id="coding")
    assert manifest.matches_profile("coding") is True
    assert manifest.matches_profile("work") is False
    assert manifest.matches_profile("companion") is False
    assert manifest.matches_profile("") is False


def test_declared_risks_match_the_task_brief() -> None:
    manifest = load_manifest(MANIFEST_PATH, expected_id="coding")
    risks = {p.tool: p.risk.value for p in manifest.permissions}
    assert risks == {
        "coding.session.start": "medium",
        "coding.session.status.read": "read",
        "coding.session.stop": "low",
        "coding.review.request": "medium",
    }


def test_test_fixture_manifest_matches_the_shipped_shape() -> None:
    """`conftest.CODING_MANIFEST` (used by every other test in this package) must stay a faithful
    stand-in for the real manifest, not drift into its own shape."""
    shipped = load_manifest(MANIFEST_PATH, expected_id="coding")
    fixture = make_manifest()
    assert {p.tool for p in fixture.permissions} == {p.tool for p in shipped.permissions}
    assert fixture.events.emits == shipped.events.emits
    assert fixture.events.listens == shipped.events.listens
    assert fixture.profiles == shipped.profiles
