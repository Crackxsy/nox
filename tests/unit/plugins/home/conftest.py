"""Fixtures for the `home` plugin's unit tests: a fake Home Assistant plus a real `PluginApi`.

The manifest is the *shipped* `plugins/home/manifest.yaml`, read from disk, so an edit to it shows
up as a test failure instead of as drift; only `network.egress` is rewritten, because a manifest
entry must be a concrete `host:port` and the fake server's port is granted by the OS.

The `home.*` settings do not come from the manifest - the plugin reads them from the configuration
layers - so the `home_plugin` fixture points `NOX_CONFIG_DEFAULTS`/`NOX_USER_CONFIG` at a
tmp_path copy rather than at the developer's own `%APPDATA%`.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from nox.ipc.protocol import Envelope, Kind, Source
from nox.plugins.api import PluginApi, PrivacyView
from nox.plugins.manifest import parse_manifest

from .fake_ha_server import VALID_TOKEN, FakeHomeAssistant

# The plugin worker inserts `plugins/home/src` into `sys.path` itself at spawn time
# (`nox.worker.plugin.load_entry`); these unit tests import `nox_plugin_home` directly in-process,
# so they need the same path entry.
REPO_ROOT = Path(__file__).resolve().parents[4]
_HOME_SRC = REPO_ROOT / "plugins" / "home" / "src"
if str(_HOME_SRC) not in sys.path:
    sys.path.insert(0, str(_HOME_SRC))

MANIFEST_PATH = REPO_ROOT / "plugins" / "home" / "manifest.yaml"
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.yaml"


class FakeClient:
    """Minimal `PluginClient`: records emitted events and answers `plugin.secret.get`."""

    def __init__(self, token: str | None = VALID_TOKEN) -> None:
        self.secrets = {"nox/home/access_token": token}
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
        """Invoke every handler registered for exactly `name`, the way the hub would deliver it."""
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

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


def manifest_dict() -> dict[str, Any]:
    return dict(yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


def make_manifest(*, port: int) -> Any:
    data = manifest_dict()
    data["network"] = {"egress": [f"127.0.0.1:{port}"]}
    return parse_manifest(data, expected_id="home")


@pytest.fixture
def home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated configuration layer - never the developer's own `%APPDATA%\\Nox\\user.yaml`."""
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(DEFAULTS_PATH))
    monkeypatch.setenv("NOX_USER_CONFIG", str(user))
    return user


def write_home_settings(user_config: Path, **values: Any) -> None:
    """Write a `home:` block into the isolated User layer."""
    raw = user_config.read_text(encoding="utf-8") if user_config.exists() else ""
    data = dict(yaml.safe_load(raw) or {}) if raw else {}
    data["home"] = {**dict(data.get("home") or {}), **values}
    user_config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_api(
    client: FakeClient,
    *,
    port: int,
    privacy: PrivacyView | None = None,
    **config_overrides: Any,
) -> PluginApi:
    manifest = make_manifest(port=port)
    config = {**manifest.config, **config_overrides}
    return PluginApi(manifest=manifest, client=client, config=config, privacy=privacy)


@pytest.fixture
async def ha_server() -> Any:
    server = FakeHomeAssistant()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
