"""User-layer (layer 2, `user.yaml`) read/write helpers shared by `nox onboard` and the dashboard.

ADR-010's four layers are Defaults -> User -> Profile/Preset -> Runtime Override. Both writers
that exist - the onboarding wizard and `config.set` - must touch exactly the same file, in the
same way, or the two would silently disagree about where a setting lives. The implementation
therefore lives here and `nox.onboarding.wizard` re-exports it.

Known limitation: the document is round-tripped through `yaml.safe_dump`, so *unknown keys are
preserved* (the patch is deep-merged into whatever is already there) but *hand-written comments
in `user.yaml` are not*. Adding a round-trip YAML dependency for that is not worth it; the file
is written by tools, and `config/defaults.yaml` - the documented, commented layer - is never
touched by either writer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from nox.core.config import NoxConfig, load_config

#: `<repo>/config/defaults.yaml` - resolved from this module, not from the working directory, so
#: a plugin worker or a test with a different cwd finds the same file the core uses.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.yaml"

USER_CONFIG_HEADER = (
    "# Nox user configuration - layer 2 of 4 (Defaults -> User -> Profile -> Runtime).\n"
    "# Written by `nox onboard` and by the dashboard's Settings page (`config.set`).\n"
    "# Never edited by the Defaults layer; never contains secrets (those live in the OS keyring).\n"
)


def default_user_config_path(appdata: Path | str | None = None) -> Path:
    """`%APPDATA%\\Nox\\user.yaml` - the same default `nox.app.build_config` falls back to."""
    base = Path(appdata) if appdata is not None else Path(os.environ["APPDATA"])
    return base / "Nox" / "user.yaml"


def resolve_defaults_path() -> Path:
    """The Defaults layer the running core uses (`NOX_CONFIG_DEFAULTS` wins, as in `nox.app`)."""
    return Path(os.environ.get("NOX_CONFIG_DEFAULTS", DEFAULTS_PATH))


def resolve_user_config_path() -> Path:
    """Where `config.set` writes: `NOX_USER_CONFIG` if set, else `%APPDATA%\\Nox\\user.yaml`.

    Unlike `nox.app.build_config` this returns the path even when the file does not exist yet -
    the point of a writer is to create it.
    """
    env_user = os.environ.get("NOX_USER_CONFIG")
    if env_user:
        return Path(env_user)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Nox" / "user.yaml"


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Nested dicts merged recursively, the rest replaced (same rule as `nox.core.config`)."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_existing_user_layer(path: Path) -> dict[str, Any]:
    """Parse `user.yaml`; an absent, empty or non-mapping file is an empty layer, never an error."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def write_user_config(path: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge `patch` into whatever `user.yaml` already has and write it back.

    Never touches `config/defaults.yaml`. Returns the merged document actually written.
    """
    existing = load_existing_user_layer(path)
    merged = deep_merge(existing, patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        USER_CONFIG_HEADER + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return merged


def load_merged_config(profile_id: str | None = None) -> NoxConfig:
    """Load Defaults + User (+ profile) exactly as the core does, from the environment alone.

    For processes that do not get a `NoxConfig` handed to them - notably a plugin worker, which
    only receives its own manifest block - and for validating a candidate `user.yaml` before
    writing it.
    """
    user_path = resolve_user_config_path()
    user = user_path if user_path.is_file() else None
    return load_config(resolve_defaults_path(), user, profile_id)
