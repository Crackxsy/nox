"""Where the Home Assistant connection settings come from.

The Plugin API only hands a worker its own manifest block, and the connection is deliberately not
in it: host, port, TLS and the room list are user settings (`home.*` in the configuration, on the
Settings page's allow-list). So the worker loads the shared Defaults + User layers itself, exactly
the way `nox_plugin_twitch.settings` does for the Twitch knobs.

On top of that, three manifest keys may *pin* this worker to one fixed instance
(:data:`PINNED_KEYS`). They are empty in the shipped manifest, for the same reason the Twitch
plugin's `channel` is: a manifest is the plugin's own declaration and the dashboard never edits
one. They exist for the two cases where a fixed value is the point - running a second worker
against a second instance, and a test against a fake one.

Nothing here is fatal. An unreadable layer or a rejected pin falls back to the shipped defaults and
says so in the log, because a plugin that cannot read a setting must still be able to report why it
is not connected.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nox.core.config import HomeConfig
from nox.plugins.api import PluginApi

#: Manifest keys that override the configuration when they carry a value.
PINNED_KEYS: tuple[str, ...] = ("host", "port", "tls")


def _from_layers(api: PluginApi) -> HomeConfig:
    """`home` from Defaults + User, or the shipped defaults if that cannot be read."""
    try:
        from nox.settings.layers import load_merged_config

        return load_merged_config().home
    except Exception as exc:  # noqa: BLE001 - a missing/invalid layer must not kill the worker
        api.log.warning("home.config_unavailable", error=type(exc).__name__)
        return HomeConfig()


def _pins(api: PluginApi) -> dict[str, Any]:
    """The manifest keys that carry a non-empty pin. `""`, `0` and `null` mean "not pinned"."""
    return {
        key: api.config[key]
        for key in PINNED_KEYS
        if key in api.config and api.config[key] not in (None, "", 0)
    }


def resolve_settings(api: PluginApi) -> HomeConfig:
    """The effective `home` configuration for this worker (see the module docstring)."""
    settings = _from_layers(api)
    pins = _pins(api)
    if not pins:
        return settings
    try:
        return HomeConfig.model_validate({**settings.model_dump(), **pins})
    except ValidationError as exc:
        api.log.warning("home.manifest_pin_invalid", keys=sorted(pins), error=str(exc))
        return settings


def websocket_url(settings: HomeConfig) -> str:
    scheme = "wss" if settings.tls else "ws"
    return f"{scheme}://{settings.host}:{settings.port}/api/websocket"
