"""Where the Twitch bot's knobs come from.

Bot names, chat rate limits and reconnect backoff used to live in `plugins/twitch/manifest.yaml`
only. A manifest is the plugin's own declaration - the dashboard never edits one - so those values
were effectively unreachable for the person running the bot. They are configuration now
(`stream.twitch.*` in `NoxConfig`, on the Settings page's allow-list) and this module is what the
plugin resolves them through.

Resolution, in order:

1. Defaults + User layer, read the way the core reads them (`nox.settings.layers.
load_merged_config`). The Plugin API only hands a worker its *own* manifest block, so the worker
loads the shared layers itself - the same thing `_resolve_channel` already did for the channel
before this change. 2. A manifest key that still carries one of the moved names overrides it, for
one release, with a deprecation log line naming the keys. That keeps an existing installation (or a
test that pins a value) working instead of silently changing its behaviour.

Nothing here is fatal: an unreadable or invalid layer falls back to the shipped defaults and says
so in the log, because a bot that cannot read a setting must still be able to connect.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from nox.core.config import StreamTwitchConfig
from nox.plugins.api import PluginApi

#: Manifest keys that moved into `stream.twitch.*`. Deprecated since, honoured for one
#: release. `channel` is deliberately not in here: it has been in both places all along, and an
#: explicitly pinned manifest channel stays a supported way to bind one instance to one channel.
MOVED_KEYS: tuple[str, ...] = (
    "bot_names",
    "relevance_cooldown_s",
    "rate_limit_max_messages",
    "rate_limit_window_s",
    "rate_limit_min_gap_s",
    "min_backoff_s",
    "max_backoff_s",
)


def _from_layers(api: PluginApi) -> StreamTwitchConfig:
    """`stream.twitch` from Defaults + User, or the shipped defaults if that cannot be read."""
    try:
        from nox.settings.layers import load_merged_config

        return load_merged_config().stream.twitch
    except Exception as exc:  # noqa: BLE001 - a missing/invalid config layer must not kill the worker
        api.log.warning("twitch.config_unavailable", error=type(exc).__name__)
        return StreamTwitchConfig()


def _manifest_overrides(api: PluginApi) -> dict[str, Any]:
    """The deprecated manifest keys this plugin was started with, if any."""
    return {key: api.config[key] for key in MOVED_KEYS if key in api.config}


def resolve_settings(api: PluginApi) -> StreamTwitchConfig:
    """The effective `stream.twitch` settings for this worker (see the module docstring)."""
    settings = _from_layers(api)
    channel = str(api.config.get("channel", "")).strip().lstrip("#")
    if not channel:
        channel = str(settings.channel).strip().lstrip("#")

    overrides = _manifest_overrides(api)
    if overrides:
        api.log.warning(
            "twitch.manifest_config_deprecated",
            keys=sorted(overrides),
            moved_to="stream.twitch",
            hint="set these in the configuration; the manifest keys are read for one more release",
        )
    try:
        return StreamTwitchConfig.model_validate(
            {**settings.model_dump(), **overrides, "channel": channel}
        )
    except ValidationError as exc:
        # A manifest that carries a value the model rejects is a packaging error, not a reason to
        # refuse to start: the configured values apply and the rejected override is named.
        api.log.warning("twitch.manifest_config_invalid", error=str(exc))
        return settings.model_copy(update={"channel": channel})
