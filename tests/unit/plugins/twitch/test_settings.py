"""Where the Twitch bot reads its knobs from (#26).

They moved out of `plugins/twitch/manifest.yaml` into `stream.twitch.*`, so what has to hold is:
the configured values are what the plugin runs with, a manifest that still carries one of the old
keys keeps working for one release (and says so in the log), and neither a missing nor a broken
configuration stops the bot from starting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from nox_plugin_twitch.plugin import TwitchPlugin
from nox_plugin_twitch.settings import MOVED_KEYS, resolve_settings

from nox.plugins.api import PluginApi

from .conftest import TWITCH_MANIFEST, FakeClient, make_manifest

#: The shipped manifest's config block: no key that moved into the configuration, and the empty
#: channel `plugins/twitch/manifest.yaml` really ships (the test manifest pins one).
SHIPPED_CONFIG: dict[str, Any] = {
    **{key: value for key, value in TWITCH_MANIFEST["config"].items() if key not in MOVED_KEYS},
    "channel": "",
}


class RecordingLog:
    """Captures what the plugin logged, so a deprecation line can be asserted on."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))

    def __getattr__(self, _name: str) -> Any:
        return lambda *args, **kwargs: None


def make_api(config: dict[str, Any], client: FakeClient | None = None) -> PluginApi:
    manifest = make_manifest(config=config)
    api = PluginApi(manifest=manifest, client=client or FakeClient())
    api.log = RecordingLog()  # type: ignore[assignment]
    return api


def warnings_of(api: PluginApi) -> list[str]:
    return [event for event, _ in api.log.warnings]  # type: ignore[attr-defined]


@pytest.fixture
def user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated User layer - never the developer's own `%APPDATA%\\Nox\\user.yaml`."""
    path = tmp_path / "user.yaml"
    monkeypatch.setenv("NOX_USER_CONFIG", str(path))
    return path


def write_user_layer(path: Path, twitch: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump({"stream": {"twitch": twitch}}), encoding="utf-8")


def test_the_configured_values_are_what_the_plugin_runs_with(user_config: Path) -> None:
    write_user_layer(
        user_config,
        {
            "channel": "mychannel",
            "bot_names": ["nox", "noxi"],
            "relevance_cooldown_s": 5.0,
            "rate_limit_max_messages": 7,
            "rate_limit_window_s": 12.0,
            "rate_limit_min_gap_s": 0.5,
            "min_backoff_s": 2.0,
            "max_backoff_s": 8.0,
        },
    )
    api = make_api(SHIPPED_CONFIG)

    settings = resolve_settings(api)

    assert settings.channel == "mychannel"
    assert settings.bot_names == ["nox", "noxi"]
    assert settings.relevance_cooldown_s == 5.0
    assert settings.rate_limit_max_messages == 7
    assert settings.rate_limit_window_s == 12.0
    assert settings.rate_limit_min_gap_s == 0.5
    assert (settings.min_backoff_s, settings.max_backoff_s) == (2.0, 8.0)
    assert warnings_of(api) == []  # nothing deprecated in a shipped manifest


def test_the_plugin_hands_the_configured_values_to_its_own_parts(user_config: Path) -> None:
    write_user_layer(
        user_config,
        {
            "channel": "mychannel",
            "bot_names": ["noxi"],
            "rate_limit_max_messages": 3,
            "rate_limit_window_s": 9.0,
            "rate_limit_min_gap_s": 0.25,
            "min_backoff_s": 2.0,
            "max_backoff_s": 8.0,
        },
    )
    plugin = TwitchPlugin(make_api(SHIPPED_CONFIG))

    assert plugin._channel == "mychannel"
    assert plugin.rate_limiter._max_messages == 3
    assert plugin.rate_limiter._window_s == 9.0
    assert plugin.rate_limiter._min_gap_s == 0.25
    assert plugin.relevance._bot_names == ["noxi"]
    assert plugin.client._min_backoff == 2.0
    assert plugin.client._max_backoff == 8.0


def test_shipped_defaults_apply_when_nothing_is_configured(user_config: Path) -> None:
    api = make_api(SHIPPED_CONFIG)

    settings = resolve_settings(api)

    assert settings.channel == ""
    assert settings.bot_names == ["nox"]
    assert settings.rate_limit_max_messages == 20
    assert settings.rate_limit_window_s == 30.0
    assert settings.rate_limit_min_gap_s == 1.5
    assert (settings.min_backoff_s, settings.max_backoff_s) == (1.0, 30.0)


def test_a_manifest_that_still_carries_a_moved_key_keeps_working_and_is_deprecated(
    user_config: Path,
) -> None:
    write_user_layer(user_config, {"rate_limit_max_messages": 7, "min_backoff_s": 2.0})
    api = make_api({**SHIPPED_CONFIG, "rate_limit_max_messages": 4, "min_backoff_s": 0.05})

    settings = resolve_settings(api)

    assert settings.rate_limit_max_messages == 4  # the manifest still wins, for one release
    assert settings.min_backoff_s == 0.05
    events = dict(api.log.warnings)  # type: ignore[attr-defined]
    assert "twitch.manifest_config_deprecated" in events
    assert events["twitch.manifest_config_deprecated"]["keys"] == [
        "min_backoff_s",
        "rate_limit_max_messages",
    ]


def test_an_invalid_manifest_value_does_not_stop_the_bot(user_config: Path) -> None:
    write_user_layer(user_config, {"channel": "mychannel", "rate_limit_max_messages": 7})
    api = make_api({**SHIPPED_CONFIG, "rate_limit_max_messages": 0})  # ge=1 on the model

    settings = resolve_settings(api)

    assert settings.rate_limit_max_messages == 7  # the configured value applies
    assert settings.channel == "mychannel"
    assert "twitch.manifest_config_invalid" in warnings_of(api)


def test_an_unreadable_configuration_falls_back_to_the_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "user.yaml"
    broken.write_text("stream: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("NOX_USER_CONFIG", str(broken))
    monkeypatch.setenv("NOX_CONFIG_DEFAULTS", str(tmp_path / "does-not-exist.yaml"))
    api = make_api(SHIPPED_CONFIG)

    settings = resolve_settings(api)

    assert settings.bot_names == ["nox"]
    assert settings.rate_limit_max_messages == 20
    assert "twitch.config_unavailable" in warnings_of(api)


def test_a_manifest_channel_still_pins_one_instance_to_one_channel(user_config: Path) -> None:
    write_user_layer(user_config, {"channel": "configured"})
    api = make_api({**SHIPPED_CONFIG, "channel": "#pinned"})

    assert resolve_settings(api).channel == "pinned"  # a leading '#' is accepted and stripped
    assert warnings_of(api) == []  # `channel` was always config *and* manifest, never deprecated
