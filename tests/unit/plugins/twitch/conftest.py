"""Fixtures shared by the `twitch` plugin's unit tests: a fake Twitch IRC server plus a real
`PluginApi` wired to it (see `tests/unit/plugins/obs/conftest.py`'s `FakeClient` pattern - no real
process, no real socket to the *core*, only to the fake IRC server)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi
from nox.plugins.manifest import parse_manifest

from .fake_irc_server import FakeIrcServer

# The plugin worker inserts `plugins/twitch/src` into `sys.path` itself at spawn time
# (`nox.worker.plugin.load_entry`); these unit tests import `nox_plugin_twitch` directly
# in-process, so they need the same path entry.
_TWITCH_SRC = Path(__file__).resolve().parents[4] / "plugins" / "twitch" / "src"
if str(_TWITCH_SRC) not in sys.path:
    sys.path.insert(0, str(_TWITCH_SRC))

TWITCH_MANIFEST: dict[str, Any] = {
    "id": "twitch",
    "name": "Twitch Bot",
    "version": "0.1.0",
    "api_version": 1,
    "entry": "nox_plugin_twitch:create",
    "profiles": ["stream"],
    "permissions": [
        {"tool": "twitch.chat.send", "risk": "low"},
        {"tool": "twitch.chat.status.read", "risk": "read"},
    ],
    "events": {
        "emits": [
            "twitch.connected",
            "twitch.disconnected",
            "twitch.chat_message",
            "twitch.command_invoked",
            "stream.funken_awarded",
            "stream.minigame_started",
            "stream.minigame_ended",
        ],
        "listens": [],
    },
    "secrets": ["nox/twitch/oauth_token", "nox/twitch/bot_username"],
    # Placeholder; `make_api` overrides this with the fake server's actual ephemeral port.
    "network": {"egress": ["127.0.0.1:6697"]},
    "resources": {"memory_mb": 128, "priority": "normal"},
    "config": {
        "channel": "testchannel",
        "host": "127.0.0.1",
        "port": 6697,
        "tls": False,
        "min_backoff_s": 0.05,
        "max_backoff_s": 0.2,
        "rate_limit_max_messages": 20,
        "rate_limit_window_s": 30.0,
        "rate_limit_min_gap_s": 1.5,
        "moderation_blocklist": [],
        "bot_names": ["nox"],
        "relevance_cooldown_s": 20.0,
        "rps_cooldown_s": 30.0,
        "rps_win_funken": 5.0,
    },
}


class FakeClient:
    """Minimal `PluginClient` (see `nox.plugins.api.PluginClient`): records emitted events, answers
    `secret.get` from `self.secrets`, and lets tests fire `client.on(...)` handlers directly."""

    def __init__(
        self, *, oauth_token: str | None = "s3cret", bot_username: str | None = "noxbot"
    ) -> None:
        self.secrets = {
            "nox/twitch/oauth_token": oauth_token,
            "nox/twitch/bot_username": bot_username,
        }
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if name == "plugin.secret.get":
            data = payload or {}
            return {"value": self.secrets.get(str(data.get("name", "")))}
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
    return parse_manifest({**TWITCH_MANIFEST, **overrides})


def make_api(client: FakeClient, *, port: int, **config_overrides: Any) -> PluginApi:
    manifest = make_manifest(
        config={**TWITCH_MANIFEST["config"], "port": port, **config_overrides},
        network={"egress": [f"127.0.0.1:{port}"]},
    )
    return PluginApi(manifest=manifest, client=client)


@pytest.fixture
async def irc_server():
    server = FakeIrcServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
