"""Fixtures for the `telegram` plugin's unit tests: the fake Bot API from `fake_telegram.py` plus a
real `PluginApi` wired to it through the real egress guard (same pattern as
`tests/unit/plugins/twitch/conftest.py` - no worker process, no socket, no `api.telegram.org`).

The manifest used here is the *real* `plugins/telegram/manifest.yaml`, read from disk, so a test
failure caused by an edit to the manifest shows up as a test failure and not as drift.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nox.plugins.api import PluginApi
from nox.plugins.manifest import load_manifest

from .fake_telegram import BOT_TOKEN, FakeTelegram

REPO_ROOT = Path(__file__).resolve().parents[4]
_TELEGRAM_SRC = REPO_ROOT / "plugins" / "telegram" / "src"
if str(_TELEGRAM_SRC) not in sys.path:
    sys.path.insert(0, str(_TELEGRAM_SRC))

MANIFEST_PATH = REPO_ROOT / "plugins" / "telegram" / "manifest.yaml"


class FakeClient:
    """Minimal `nox.plugins.api.PluginClient`: records emitted events and answers `secret.get`."""

    def __init__(self, *, bot_token: str | None = BOT_TOKEN) -> None:
        self.secrets: dict[str, str | None] = {"nox/telegram/bot_token": bot_token}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Any]] = {}

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        if name == "plugin.secret.get":
            return {"value": self.secrets.get(str((payload or {}).get("name", "")))}
        return {}

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        self.events.append((name, dict(payload or {})))

    def on(self, name_glob: str, handler: Any) -> Any:
        self.handlers.setdefault(name_glob, []).append(handler)
        return lambda: self.handlers[name_glob].remove(handler)

    def emitted(self, name: str) -> list[dict[str, Any]]:
        return [payload for event_name, payload in self.events if event_name == name]


def make_api(client: FakeClient, telegram: FakeTelegram, **config_overrides: Any) -> PluginApi:
    manifest = load_manifest(MANIFEST_PATH, expected_id="telegram")
    config = {**manifest.config, **config_overrides}
    return PluginApi(
        manifest=manifest,
        client=client,
        config=config,
        transport_factory=telegram.transport,
    )


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def plugin(fake_client: FakeClient, telegram: FakeTelegram):
    from nox_plugin_telegram import create

    return create(make_api(fake_client, telegram, poll_timeout_s=0, request_timeout_s=2.0))
