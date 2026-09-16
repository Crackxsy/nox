"""Fixtures shared by the `obs` plugin's unit tests: a fake obs-websocket v5 server plus a real
`PluginApi` wired to it (see `tests/unit/plugins/test_api.py`'s `FakeClient` pattern for the IPC
side - no real process, no real socket to the *core*, only to the fake OBS server)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi
from nox.plugins.manifest import parse_manifest

from .fake_obs_server import FakeObsServer

# The plugin worker inserts `plugins/obs/src` into `sys.path` itself at spawn time
# (`nox.worker.plugin.load_entry`); these unit tests import `nox_plugin_obs` directly in-process,
# so they need the same path entry.
_OBS_SRC = Path(__file__).resolve().parents[4] / "plugins" / "obs" / "src"
if str(_OBS_SRC) not in sys.path:
    sys.path.insert(0, str(_OBS_SRC))

OBS_MANIFEST: dict[str, Any] = {
    "id": "obs",
    "name": "OBS Studio",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_obs:create",
    "profiles": ["stream"],
    "permissions": [
        {"tool": "obs.status.read", "risk": "read"},
        {"tool": "obs.scenes.list", "risk": "read"},
        {"tool": "obs.scene.switch", "risk": "medium"},
        {"tool": "obs.privacy_scene.activate", "risk": "medium"},
        {"tool": "obs.preflight.check", "risk": "read"},
        {"tool": "obs.replay_buffer.save", "risk": "medium"},
        {"tool": "obs.replay_buffer.status.read", "risk": "read"},
    ],
    "events": {
        "emits": [
            "obs.connected",
            "obs.disconnected",
            "obs.scene_changed",
            "stream.started",
            "stream.ended",
            "obs.recording_changed",
        ],
        "listens": ["security.panic"],
    },
    "secrets": ["nox/obs/websocket_password"],
    # Placeholder; `make_api` overrides this with the fake server's actual ephemeral port (a
    # manifest `network.egress` entry must be a concrete host:port, no wildcards).
    "network": {"egress": ["127.0.0.1:4455"]},
    "resources": {"memory_mb": 128, "priority": "normal"},
    "config": {
        "host": "127.0.0.1",
        "port": 4455,
        "privacy_scene": "Technical Problems",
        "scene_set": ["Start", "Live", "Pause", "Technical Problems"],
        "subscribe_events": True,
        "min_backoff_s": 0.05,
        "max_backoff_s": 0.2,
    },
}


class FakeClient:
    """Minimal `PluginClient` (see `nox.plugins.api.PluginClient`): records emitted events, answers
    `secret.get` from `self.secret`, and lets tests fire `client.on(...)` handlers directly."""

    def __init__(self, secret: str | None = "s3cret") -> None:
        self.secret = secret
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if name == "plugin.secret.get":
            return {"value": self.secret}
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
        """Test helper: invoke every handler registered for exactly `name` (unit tests only use
        exact patterns, matching `manifest.events.listens`). `PluginEventsApi.on` wraps handlers as
        `_forward(env: Envelope)`, so the fake has to build a real `Envelope`, same as the hub
        would deliver."""
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
    return parse_manifest({**OBS_MANIFEST, **overrides})


def make_api(client: FakeClient, *, port: int, **config_overrides: Any) -> PluginApi:
    # `network.egress` entries must be a concrete `host:port` (no wildcards - manifest.py's
    # `split_endpoint` rejects "*"), so it is built per test from the fixture's ephemeral port.
    manifest = make_manifest(
        config={**OBS_MANIFEST["config"], "port": port, **config_overrides},
        network={"egress": [f"127.0.0.1:{port}"]},
    )
    return PluginApi(manifest=manifest, client=client)


@pytest.fixture
async def obs_server():
    server = FakeObsServer(password="s3cret")
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient(secret="s3cret")
