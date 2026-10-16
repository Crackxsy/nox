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
import sys
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


def nox_home() -> Path:
    """The one folder Nox owns on this machine: user config, database, runtime files, models.

    `NOX_HOME` wins (the Raspberry Pi service sets it to `/var/lib/nox`). Otherwise the platform's
    place for per-user application data: `%APPDATA%/Nox` on Windows, `~/Library/Application
    Support/Nox` on macOS, `$XDG_DATA_HOME/Nox` (default `~/.local/share/Nox`) elsewhere. An
    `APPDATA` variable is honoured on every system, so a test that sets it gets the Windows layout
    it asked for.

    Always absolute: the core, the CLI and every worker start in different directories, and a
    relative `NOX_HOME` would give each of them a different folder.
    """
    env = os.environ.get("NOX_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "Nox"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "Nox"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Nox"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "Nox"


def user_config_path() -> Path:
    """Where the user layer is *written*; it need not exist yet.

    `NOX_USER_CONFIG` wins, otherwise `user.yaml` in `nox_home()`.
    """
    env = os.environ.get("NOX_USER_CONFIG")
    if env:
        return Path(env)
    return nox_home() / USER_CONFIG_FILENAME


def resolve_config_paths(user_config: Path | None = None) -> tuple[Path, Path | None]:
    """`(defaults, user or None)` for `nox.core.config.load_config`.

    An explicit `user_config` wins, then `NOX_USER_CONFIG`, then `user.yaml` in `nox_home()`, then
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
    candidates = [nox_home() / USER_CONFIG_FILENAME, defaults.parent / USER_CONFIG_FILENAME]
    for candidate in candidates:
        if candidate.is_file():
            return defaults, candidate
    return defaults, None
