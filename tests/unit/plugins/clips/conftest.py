"""Fixtures shared by the `clips` plugin's unit tests: a real `PluginApi` wired to a `FakeClient`
(no real process, no real socket - same pattern as `tests/unit/plugins/obs/conftest.py`)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi
from nox.plugins.manifest import parse_manifest

_CLIPS_SRC = Path(__file__).resolve().parents[4] / "plugins" / "clips" / "src"
if str(_CLIPS_SRC) not in sys.path:
    sys.path.insert(0, str(_CLIPS_SRC))

CLIPS_MANIFEST: dict[str, Any] = {
    "id": "clips",
    "name": "Clip Pipeline",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_clips:create",
    "profiles": ["stream", "coding", "companion"],
    "permissions": [],
    "events": {
        "emits": ["clip.requested"],
        "listens": [
            "rl.event",
            "twitch.chat_mood_changed",
            "twitch.command_invoked",
            "stream.started",
            "stream.ended",
            "stream.mode_changed",
            "security.kill_switch",
            "security.panic",
        ],
    },
    "secrets": [],
    "network": {"egress": []},
    "resources": {"memory_mb": 64, "priority": "low"},
    "config": {
        "highlight_kinds": ["goal", "save", "win", "overtime"],
        "chat_hype_threshold": 0.75,
        "cooldown_s": 15.0,
        "manual_lookback_s": 20.0,
    },
}


class FakeClient:
    """Minimal `PluginClient`: records emitted events and lets tests fire `client.on(...)`
    handlers directly (see `tests/unit/plugins/obs/conftest.py` for the same pattern)."""

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
    return parse_manifest({**CLIPS_MANIFEST, **overrides})


def make_api(client: FakeClient, **config_overrides: Any) -> PluginApi:
    manifest = make_manifest(config={**CLIPS_MANIFEST["config"], **config_overrides})
    return PluginApi(manifest=manifest, client=client)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
