"""Runtime files the shell reads/writes: session.token, ipc.json, supervisor.token, shell.json.

Location: `<app_dir>/runtime` by default (`nox.paths.app_dir`: `%APPDATA%\\Nox` on Windows, the
platform's application folder elsewhere), overridable with `NOX_RUNTIME_DIR`. It matches the
`paths.runtime_dir` default, which deliberately does not follow `paths.data_dir`. Tokens are read
from files only and never logged.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from nox.paths import app_dir

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
    """Persisted UI state: lives in the runtime directory, never holds secrets."""

    x: int | None = None
    y: int | None = None
    width: int = 260
    height: int = 300
    visible: bool = True
    click_through: bool = False


def candidate_runtime_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("NOX_RUNTIME_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(app_dir() / "runtime")
    return dirs


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


class DashboardTicketError(RuntimeError):
    """The core did not hand out a dashboard ticket (offline, or it refused the token)."""


#: The core's endpoint for one-time dashboard tickets (`nox.ipc.http`).
DASHBOARD_TICKET_PATH = "/api/ui/dashboard-ticket"


def request_dashboard_ticket(
    http_port: int, host: str, session_token: str, *, timeout_s: float = 2.0
) -> str:
    """Ask the core for a one-time dashboard ticket, with the shell's token as bearer.

    Loopback only and never through a proxy: `urllib` would otherwise honour `HTTP_PROXY` and send
    the session token to whatever proxy the environment names.
    """
    request = urllib.request.Request(  # noqa: S310 - fixed http://<loopback> URL built here
        f"http://{host}:{http_port}{DASHBOARD_TICKET_PATH}",
        method="POST",
        headers={"Authorization": f"Bearer {session_token}"},
        data=b"",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise DashboardTicketError(f"no dashboard ticket: {type(exc).__name__}") from exc
    ticket = body.get("ticket") if isinstance(body, dict) else None
    if not isinstance(ticket, str) or not ticket:
        raise DashboardTicketError("the core answered without a ticket")
    return ticket


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


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Read the YAML config tree (defaults only; the core owns the full 4-layer merge).

    Order: explicit path, `NOX_CONFIG`, `<repo>/config/defaults.yaml`. Missing file -> {}.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(path)
    env = os.environ.get("NOX_CONFIG")
    if env:
        candidates.append(Path(env))
    candidates.append(Path(__file__).resolve().parents[3] / "config" / "defaults.yaml")
    for c in candidates:
        try:
            data = yaml.safe_load(c.read_text(encoding="utf-8"))
        except OSError:
            continue
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}
