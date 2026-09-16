"""Fixtures for the `creative` plugin's unit tests: a real `PluginApi` wired to a `FakeClient`
(no real process, no real socket - same pattern as `tests/unit/plugins/clips/conftest.py` and
`tests/unit/plugins/obs/conftest.py`)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi
from nox.plugins.manifest import parse_manifest

_CREATIVE_SRC = Path(__file__).resolve().parents[4] / "plugins" / "creative" / "src"
if str(_CREATIVE_SRC) not in sys.path:
    sys.path.insert(0, str(_CREATIVE_SRC))

CREATIVE_MANIFEST: dict[str, Any] = {
    "id": "creative",
    "name": "Creative Apps",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_creative:create",
    "profiles": [],
    "permissions": [
        {"tool": "creative.artifact.inspect", "risk": "low"},
        {"tool": "creative.screenshot.analyze", "risk": "medium"},
    ],
    "events": {
        "emits": [
            "creative.app_detected",
            "creative.app_left",
            "creative.note_written",
            "creative.screenshot.requested",
        ],
        "listens": ["sensor.foreground_changed", "creative.screenshot.result"],
    },
    "secrets": [],
    "network": {"egress": []},
    "resources": {"memory_mb": 128, "priority": "normal"},
    "config": {
        "hysteresis_s": 15.0,
        "vault_folder": "16 - Creative Projects",
        "app_patterns": {
            "blender": [{"process": "blender.exe"}],
            "fl_studio": [{"process": "fl64.exe"}, {"process": "fl.exe"}],
            "krita": [{"process": "krita.exe"}],
            "capcut": [{"process": "capcut.exe"}],
            "davinci_resolve": [{"process": "resolve.exe"}],
            "browser_daw": [
                {"process": "chrome.exe", "title": "*Web DAW*"},
                {"process": "msedge.exe", "title": "*Web DAW*"},
                {"process": "firefox.exe", "title": "*Web DAW*"},
            ],
        },
    },
}


class FakeClient:
    """Minimal `PluginClient`: records emitted events and lets tests fire `client.on(...)`
    handlers directly."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return {}

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        self.events.append((name, dict(payload or {})))

    def on(self, name_glob: str, handler: Any) -> Any:
        self.handlers.setdefault(name_glob, []).append(handler)

        def _unsub() -> None:
            self.handlers[name_glob].remove(handler)

        return _unsub

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


def make_manifest(**overrides: Any):
    return parse_manifest({**CREATIVE_MANIFEST, **overrides})


def make_api(client: FakeClient, **config_overrides: Any) -> PluginApi:
    manifest = make_manifest(config={**CREATIVE_MANIFEST["config"], **config_overrides})
    return PluginApi(manifest=manifest, client=client)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
