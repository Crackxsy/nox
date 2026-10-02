"""Runtime files the shell reads/writes: session.token, ipc.json, supervisor.token, shell.json.

Location (IPC Model handshake step 2 / D223): `%APPDATA%\\Nox\\runtime` by default, overridable
with `NOX_RUNTIME_DIR` or with `paths.runtime_dir` in the user configuration. Tokens are
read from files only and never logged.

The shell is a separate process from the core, so it has to find the directory the core
actually wrote to. It reads `paths.runtime_dir` rather than guessing: a moved runtime
directory used to leave the shell waiting in the default location for a token that was
never going to appear there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from nox.core.config.types import ConfigError, expand_path
from nox.paths import resolve_config_paths, user_config_path

SESSION_TOKEN_FILE = "session.token"  # noqa: S105 - file name, not a secret
SUPERVISOR_TOKEN_FILE = "supervisor.token"  # noqa: S105
IPC_FILE = "ipc.json"
SHELL_STATE_FILE = "shell.json"
DEFAULT_SUPERVISOR_PORT = 47799


class IpcEndpoints(BaseModel):
    ws_port: int = Field(ge=1, le=65535)
    http_port: int = Field(ge=1, le=65535)
    host: str = "127.0.0.1"
    supervisor_port: int = DEFAULT_SUPERVISOR_PORT


class ShellState(BaseModel):
    """Persisted UI state (D223: UI state lives in AppData, never secrets)."""

    x: int | None = None
    y: int | None = None
    width: int = 260
    height: int = 300
    visible: bool = True
    click_through: bool = False


def _configured_runtime_dir() -> Path | None:
    """`paths.runtime_dir` from the user configuration, or `None` when it sets none.

    Only the user layer is read, and as plain YAML rather than validated: the default location is
    already the last candidate below, and the shell has no business refusing to start because some
    unrelated section of the configuration is malformed.
    """
    try:
        raw = yaml.safe_load(user_config_path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    paths = raw.get("paths") if isinstance(raw, dict) else None
    configured = paths.get("runtime_dir") if isinstance(paths, dict) else None
    if not isinstance(configured, str) or not configured.strip():
        return None
    try:
        return expand_path(configured)
    except ConfigError:
        return None


def candidate_runtime_dirs() -> list[Path]:
    """Where the runtime files may be, most specific first."""
    dirs: list[Path] = []
    env = os.environ.get("NOX_RUNTIME_DIR")
    if env:
        dirs.append(Path(env))
    configured = _configured_runtime_dir()
    if configured is not None:
        dirs.append(configured)
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "Nox" / "runtime")
    # A machine without APPDATA is not Windows and not a supported install, but the shell still
    # has to return somewhere rather than an empty list its caller would index into.
    return dirs or [Path.cwd() / "runtime"]


def resolve_runtime_dir(candidates: list[Path] | None = None) -> Path:
    """First candidate that already holds a session token, else the first candidate."""
    dirs = candidates if candidates is not None else candidate_runtime_dirs()
    for d in dirs:
        if (d / SESSION_TOKEN_FILE).is_file():
            return d
    return dirs[0]


def _read_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def read_session_token(runtime_dir: Path) -> str | None:
    return _read_token(runtime_dir / SESSION_TOKEN_FILE)


def read_supervisor_token(runtime_dir: Path) -> str | None:
    return _read_token(runtime_dir / SUPERVISOR_TOKEN_FILE)


def read_ipc_endpoints(runtime_dir: Path) -> IpcEndpoints | None:
    """None when ipc.json is missing or unreadable; the shell then runs offline."""
    try:
        raw = json.loads((runtime_dir / IPC_FILE).read_text(encoding="utf-8"))
        return IpcEndpoints.model_validate(raw)
    except (OSError, ValueError, ValidationError):
        return None


def load_shell_state(runtime_dir: Path) -> ShellState:
    try:
        raw = json.loads((runtime_dir / SHELL_STATE_FILE).read_text(encoding="utf-8"))
        return ShellState.model_validate(raw)
    except (OSError, ValueError, ValidationError):
        return ShellState()


def save_shell_state(runtime_dir: Path, state: ShellState) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    tmp = runtime_dir / (SHELL_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
    os.replace(tmp, runtime_dir / SHELL_STATE_FILE)


def _read_yaml(path: Path) -> dict[str, Any] | None:
    """A file's YAML mapping; None when it cannot be read, {} when it is not a mapping."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Nested mappings merged recursively, everything else replaced (the core's rule)."""
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        merged[key] = (
            _merge(current, value)
            if isinstance(value, dict) and isinstance(current, dict)
            else value
        )
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    """The configuration the shell acts on: the defaults with the user's own layer on top.

    An explicit `path` is read on its own (tests). Otherwise the defaults come from `NOX_CONFIG`
    or the shipped `config/defaults.yaml`, and the user layer from the same resolver the core and
    the supervisor use (`nox.paths.resolve_config_paths`). Reading the defaults alone left the
    shell blind to every setting the user changed - the pet window always opened as the plain
    shape, whatever creature was chosen in the dashboard.
    """
    if path is not None:
        return _read_yaml(path) or {}
    env = os.environ.get("NOX_CONFIG")
    defaults_file = (
        Path(env) if env else Path(__file__).resolve().parents[3] / "config" / "defaults.yaml"
    )
    config = _read_yaml(defaults_file) or {}
    _defaults, user_file = resolve_config_paths()
    if user_file is not None:
        config = _merge(config, _read_yaml(user_file) or {})
    return config
