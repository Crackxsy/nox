"""Where Nox finds the files that ship with it: the config layers, the profiles, the plugin
folder and the built UI bundles.

Every process (core, supervisor, CLI, onboarding wizard, settings editor) resolves these through
this module, so they cannot disagree about which `defaults.yaml` or `user.yaml` is in force. The
locations are derived from this file's own position rather than from the working directory,
because a plugin worker, a test and a service start all run with a different one.

Nothing here reads or validates a file; `nox.core.config` does that.
"""

from __future__ import annotations

import os
from pathlib import Path

#: `<...>/src/nox`
PACKAGE_DIR = Path(__file__).resolve().parent


def repo_root() -> Path:
    """The directory that holds `config/`, `plugins/` and `ui/`.

    Running from a checkout, that is the repository root. `NOX_REPO_ROOT` overrides it, which is
    how an installed copy points at the data directory it was deployed with. If neither has a
    `config/defaults.yaml`, the package directory is returned, so the caller fails on a path it
    can print rather than on a silently wrong one two layers down.
    """
    override = os.environ.get("NOX_REPO_ROOT")
    if override:
        return Path(override)
    checkout = PACKAGE_DIR.parents[1]
    if (checkout / "config" / "defaults.yaml").is_file():
        return checkout
    return PACKAGE_DIR


REPO_ROOT = repo_root()
CONFIG_DIR = REPO_ROOT / "config"
DEFAULTS_PATH = CONFIG_DIR / "defaults.yaml"
PROFILES_DIR = CONFIG_DIR / "profiles"
PLUGINS_DIR = REPO_ROOT / "plugins"
PET_DIST = REPO_ROOT / "ui" / "pet" / "dist"
DASHBOARD_DIST = REPO_ROOT / "ui" / "dashboard" / "dist"

USER_CONFIG_FILENAME = "user.yaml"


def defaults_path() -> Path:
    """The defaults layer in force. `NOX_CONFIG_DEFAULTS` overrides the shipped file."""
    env = os.environ.get("NOX_CONFIG_DEFAULTS")
    return Path(env) if env else DEFAULTS_PATH


def user_config_path() -> Path:
    """Where the user layer is *written*; it need not exist yet.

    `NOX_USER_CONFIG` wins, otherwise `%APPDATA%\\Nox\\user.yaml`, falling back to the equivalent
    under the home directory when `APPDATA` is not set.
    """
    env = os.environ.get("NOX_USER_CONFIG")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Nox" / USER_CONFIG_FILENAME


def resolve_config_paths(user_config: Path | None = None) -> tuple[Path, Path | None]:
    """`(defaults, user or None)` for `nox.core.config.load_config`.

    An explicit `user_config` wins, then `NOX_USER_CONFIG`, then `%APPDATA%\\Nox\\user.yaml`, then
    a `user.yaml` next to the defaults file, which is the layout a checkout uses. The second
    element is None when no user layer exists, which the loader reads as "defaults only".

    Core and supervisor both call this. Two resolvers with different fallbacks meant the watchdog
    could run on a different configuration than the process it watches.
    """
    defaults = defaults_path()
    if user_config is not None:
        return defaults, user_config
    env_user = os.environ.get("NOX_USER_CONFIG")
    if env_user:
        return defaults, Path(env_user)
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Nox" / USER_CONFIG_FILENAME)
    candidates.append(defaults.parent / USER_CONFIG_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return defaults, candidate
    return defaults, None
