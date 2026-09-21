"""Where the worker's connection settings come from, and what happens when they cannot be read.

The shipped manifest must not pin an address: a plugin that ships pointing at somebody's own IP
would connect to the wrong house on every other installation. The pins exist for a second instance
and for a test, and the configuration is the normal source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import FakeClient, make_api, manifest_dict, write_home_settings

pytestmark = pytest.mark.timeout(30)


class RecordingLog:
    """Captures what the plugin logged, so a fallback line can be asserted on."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))

    def __getattr__(self, _name: str) -> Any:
        return lambda *args, **kwargs: None


def test_the_shipped_manifest_pins_no_address() -> None:
    config = manifest_dict()["config"]
    assert config["host"] == ""
    assert config["port"] == 0


def test_the_configuration_is_the_normal_source(home_config: Path) -> None:
    from nox_plugin_home.settings import resolve_settings, websocket_url

    write_home_settings(home_config, host="192.168.1.50", port=8123, tls=True)
    settings = resolve_settings(make_api(FakeClient(), port=8123))
    assert settings.host == "192.168.1.50"
    assert websocket_url(settings) == "wss://192.168.1.50:8123/api/websocket"


def test_a_manifest_pin_overrides_the_configuration(home_config: Path) -> None:
    from nox_plugin_home.settings import resolve_settings

    write_home_settings(home_config, host="192.168.1.50", port=8123)
    api = make_api(FakeClient(), port=8123)
    api.config["host"] = "127.0.0.1"
    api.config["port"] = 9123
    settings = resolve_settings(api)
    assert (settings.host, settings.port) == ("127.0.0.1", 9123)


def test_an_invalid_pin_does_not_stop_the_worker(home_config: Path) -> None:
    """A packaging error must not become "Nox will not start"; the configured values apply."""
    from nox_plugin_home.settings import resolve_settings

    write_home_settings(home_config, host="192.168.1.50", port=8123)
    api = make_api(FakeClient(), port=8123)
    api.config["port"] = 99999  # outside the model's bounds
    api.log = RecordingLog()  # type: ignore[assignment]
    settings = resolve_settings(api)
    assert (settings.host, settings.port) == ("192.168.1.50", 8123)
    assert [event for event, _ in api.log.warnings] == ["home.manifest_pin_invalid"]


def test_an_unreadable_configuration_falls_back_to_the_shipped_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nox_plugin_home.settings import resolve_settings

    broken = tmp_path / "user.yaml"
    broken.write_text("home: [this is not a mapping", encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(broken))
    api = make_api(FakeClient(), port=8123)
    api.log = RecordingLog()  # type: ignore[assignment]
    settings = resolve_settings(api)
    # The shipped default, and a log line naming why - never a crash and never a guessed address.
    assert settings.host == "127.0.0.1"
    assert settings.port == 8123
