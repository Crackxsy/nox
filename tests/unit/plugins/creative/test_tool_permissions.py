"""Manifest validity and tool-registry negative test (Spec v0.7 §8/§9 acceptance criteria,
Plan v0.7 DoD): no `mouse.*`/`keyboard.*`/UI-automation tool exists anywhere in this plugin, and
every registered tool's risk matches its manifest permission exactly."""

from __future__ import annotations

from pathlib import Path

from nox_plugin_creative import create

from nox.plugins.manifest import load_manifest
from nox.security.model import Risk

from .conftest import FakeClient, make_api

MANIFEST_PATH = Path(__file__).resolve().parents[4] / "plugins" / "creative" / "manifest.yaml"


class TestManifest:
    def test_loads_and_validates(self) -> None:
        manifest = load_manifest(MANIFEST_PATH)
        assert manifest.id == "creative"
        assert manifest.entry == "nox_plugin_creative:create"

    def test_declares_no_ui_automation_tools(self) -> None:
        manifest = load_manifest(MANIFEST_PATH)
        for permission in manifest.permissions:
            assert not permission.tool.startswith("mouse.")
            assert not permission.tool.startswith("keyboard.")
            assert "automation" not in permission.tool

    def test_declares_no_egress(self) -> None:
        """Local-only: app-family detection and artefact metadata never leave the machine."""
        manifest = load_manifest(MANIFEST_PATH)
        assert manifest.network.egress == []

    def test_not_enabled_by_default(self) -> None:
        import yaml

        defaults = yaml.safe_load(
            (Path(__file__).resolve().parents[4] / "config" / "defaults.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert "creative" not in defaults["plugins"]["enabled"]


class TestToolRegistry:
    def test_registers_exactly_the_two_tools(self) -> None:
        api = make_api(FakeClient())
        create(api)
        assert api.tools.names() == ["creative.artifact.inspect", "creative.screenshot.analyze"]

    def test_no_tool_targets_mouse_or_keyboard(self) -> None:
        """Registry-level twin of the manifest test: even a future tool added only in code, not
        the manifest, would be caught here (Plan v0.7 DoD)."""
        api = make_api(FakeClient())
        create(api)
        for name in api.tools.names():
            assert not name.startswith("mouse.")
            assert not name.startswith("keyboard.")

    def test_risk_matches_manifest(self) -> None:
        api = make_api(FakeClient())
        create(api)
        assert api.tools.get("creative.artifact.inspect").risk is Risk.LOW
        assert api.tools.get("creative.screenshot.analyze").risk is Risk.MEDIUM
        assert api.tools.get("creative.screenshot.analyze").side_effects is True
        assert api.tools.get("creative.artifact.inspect").side_effects is False
