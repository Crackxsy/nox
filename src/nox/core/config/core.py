"""Sections that describe the installation itself: who Nox is, where it keeps its files, how it
talks to its own UIs, and how it is logged, watched and extended.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from nox.core.config.types import (
    ExpandedPath,
    OptionalExpandedPath,
    StrictSection,
    expand_path,
    require_ordered,
)

__all__ = [
    "DATA_SUBDIRECTORIES",
    "DEFAULT_DATA_DIR",
    "HealthConfig",
    "IdentityConfig",
    "IpcConfig",
    "LanConfig",
    "LocaleConfig",
    "LoggingConfig",
    "PathsConfig",
    "PluginsConfig",
    "SupervisorConfig",
]


# ---- identity -----------------------------------------------------------------------------------


class LocaleConfig(StrictSection):
    time_format: str = "24h"
    date_format: str = "DD.MM.YYYY"
    timezone: str = "Europe/Berlin"
    units: Literal["metric", "imperial"] = "metric"


class IdentityConfig(StrictSection):
    name: str = "Nox"
    user_display_name: str = ""
    ui_language: Literal["de", "en"] = "de"
    speech_language: Literal["de", "en", "auto"] = "auto"
    locale: LocaleConfig = Field(default_factory=LocaleConfig)


# ---- paths --------------------------------------------------------------------------------------

#: Where an installation keeps everything unless the user moves it.
DEFAULT_DATA_DIR = "${NOX_HOME}"

#: Every directory below `data_dir` and the sub-directory it gets. Setting one of these keys
#: explicitly moves just that one; setting `data_dir` moves all of them, which is what the
#: onboarding wizard relies on when it asks for a single data folder.
DATA_SUBDIRECTORIES: Mapping[str, str] = {
    "vault_dir": "vault",
    "index_dir": "index",
    "database_dir": "database",
    "cache_dir": "cache",
    "backups_dir": "backups",
    "logs_dir": "logs",
}

#: The runtime directory deliberately does **not** follow `data_dir`.
#:
#: It is the rendezvous point between processes, and the desktop shell and the pet window look for
#: it before they have read any configuration - they only need the session token and the port file
#: that the core leaves there. A rendezvous point that moves with a user setting is one that two
#: processes can disagree about, and they did: with `data_dir` on another drive the core wrote its
#: token to the new location while the shell kept reading the old one and was refused at the hub.
#:
#: It holds no user data - a per-start token and a port file, both recreated on every boot - so
#: keeping it in the fixed application-data location costs nothing and removes the disagreement.
#: Set `paths.runtime_dir` explicitly to move it anyway.
DEFAULT_RUNTIME_DIR = "${NOX_HOME}/runtime"


class PathsConfig(StrictSection):
    """Filesystem locations. Everything lives below `data_dir` unless it is overridden by name."""

    data_dir: ExpandedPath = Path(DEFAULT_DATA_DIR)
    vault_dir: ExpandedPath
    index_dir: ExpandedPath
    database_dir: ExpandedPath
    cache_dir: ExpandedPath
    backups_dir: ExpandedPath
    runtime_dir: ExpandedPath = Path(DEFAULT_RUNTIME_DIR)
    logs_dir: ExpandedPath

    @model_validator(mode="before")
    @classmethod
    def _fill_from_data_dir(cls, data: Any) -> Any:
        """Supply every unset directory as `<data_dir>/<name>` before the fields are validated."""
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        base = expand_path(str(values.get("data_dir") or DEFAULT_DATA_DIR))
        for field, subdirectory in DATA_SUBDIRECTORIES.items():
            if not values.get(field):
                values[field] = base / subdirectory
        return values


# ---- ipc ----------------------------------------------------------------------------------------


class LanConfig(StrictSection):
    """Network access (`nox.lan`): the dashboard for paired devices in the home network.

    Off unless asked for. When on, a second listener serves the dashboard over HTTPS (a
    per-install certificate authority, see `nox.lan.tls`) to devices paired with a one-time code;
    the loopback listeners above stay exactly as they are.
    """

    enabled: bool = False
    #: All local addresses by default, so phones and laptops in the home network reach it.
    host: str = "0.0.0.0"  # noqa: S104 - binding the home network is this listener's purpose
    #: 0 lets the operating system pick a free port (tests); the one in use is in `lan.status`.
    port: int = Field(default=47843, ge=0, le=65535)
    max_devices: int = Field(default=20, ge=1, le=100)
    pair_code_ttl_s: float = Field(default=300.0, ge=60.0, le=3600.0)
    #: Pairing attempts per source address and minute; a wrong code costs one.
    pair_attempts_per_minute: int = Field(default=5, ge=1, le=60)
    #: Serve clients from public addresses too. Off: only loopback and private networks are
    #: answered, so a port forward or a public address on the machine does not expose the
    #: pairing page to the internet. Access from outside the home is its own feature (wave F).
    allow_public_clients: bool = False
    #: Where `nox node`s connect (mutual TLS, `nox.node.listener`); only while `enabled`.
    node_port: int = Field(default=47844, ge=0, le=65535)
    #: Paired nodes at most (PCs, laptops, a second Pi).
    max_nodes: int = Field(default=10, ge=1, le=50)


class IpcConfig(StrictSection):
    host: str = "127.0.0.1"
    port: int = Field(default=47800, ge=1024, le=65535)
    http_port: int = Field(default=47801, ge=1024, le=65535)
    schema_version: int = Field(default=1, ge=1)
    auth: Literal["token"] = "token"
    lan: LanConfig = Field(default_factory=LanConfig)


# ---- health, logging, supervisor, plugins -------------------------------------------------------


class HealthConfig(StrictSection):
    check_interval_s: float = Field(default=30.0, gt=0.0)
    checkpoint_interval_s: float = Field(default=5.0, gt=0.0)


class LoggingConfig(StrictSection):
    model_config = ConfigDict(extra="forbid", validate_default=True, populate_by_name=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = Field(default=True, alias="json")  # the YAML key is `json`
    pii_filter: bool = True
    retention_days: int | None = Field(default=None, ge=1)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class PluginsConfig(StrictSection):
    """Which plugin workers the core starts, and where their `<id>/manifest.yaml` folders live."""

    enabled: list[str] = Field(default_factory=list)  # plugin ids; empty = no plugin is started
    dir: OptionalExpandedPath = None  # unset = the `plugins` folder shipped with Nox


class SupervisorConfig(StrictSection):
    restart_limit: int = Field(default=3, ge=0)
    restart_window_s: float = Field(default=300.0, gt=0.0)
    kill_switch_hotkey: str = "ctrl+alt+shift+k"
    control_host: str = "127.0.0.1"
    control_port: int = Field(default=47799, ge=1024, le=65535)
    heartbeat_interval_s: float = Field(default=2.0, gt=0.0)
    #: Cold boots take 20-40 s on a busy machine, so no missed-heartbeat accounting happens until
    #: the core has sent its first heartbeat or this grace since spawn has elapsed. Counting from
    #: spawn time restarted cores that were merely still importing.
    boot_grace_s: float = Field(default=90.0, gt=0.0)
    missed_for_graceful: int = Field(default=5, ge=1)
    missed_for_hard: int = Field(default=10, ge=1)
    kill_ack_timeout_s: float = Field(default=2.0, gt=0.0)
    stop_timeout_s: float = Field(default=6.0, gt=0.0)
    core_command: list[str] = Field(default_factory=lambda: ["python", "-m", "nox.app"])
    shell_command: list[str] = Field(default_factory=lambda: ["python", "-m", "nox.shell"])
    shell_enabled: bool = True

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> SupervisorConfig:
        return require_ordered(self, "missed_for_graceful", "missed_for_hard")
