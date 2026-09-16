"""Supervisor control-channel protocol: newline-delimited JSON `Envelope`s over localhost TCP.

Names (Process Model §Supervisor): `sup.auth` (first frame on every connection: `{token, role,
pid}`), `sup.auth_ok` (response), `sup.heartbeat` (core -> supervisor, event), `sup.kill`
(supervisor -> core request; also shell/tray -> supervisor request), `sup.stop` (shell/tray ->
supervisor request, and supervisor -> core request: `{reason}`, B-6 graceful shutdown), `sup.ack`
(response), `sup.status` (request/response), `sup.resume` (request: leave safe mode), `sup.error`
(error).

B-1 (handshake-once auth): a connection authenticates exactly once, with the first frame it sends.
That frame must be `sup.auth`; the supervisor checks `payload.token` and replies `sup.auth_ok` (or
`sup.error`/close on a wrong token). Every frame after that carries no token at all - the connection
itself is the credential. Frames received before authentication succeeds are dropped and logged
once per connection; they never reach message handling.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nox.ipc.protocol import Envelope, Kind, Source

NAME_AUTH = "sup.auth"
NAME_AUTH_OK = "sup.auth_ok"
NAME_HEARTBEAT = "sup.heartbeat"
NAME_KILL = "sup.kill"
NAME_STOP = "sup.stop"
NAME_ACK = "sup.ack"
NAME_STATUS = "sup.status"
NAME_RESUME = "sup.resume"
NAME_ERROR = "sup.error"

TOKEN_FILE = "supervisor.token"  # noqa: S105 - file name, not a secret
ENV_HOST = "NOX_SUPERVISOR_HOST"
ENV_PORT = "NOX_SUPERVISOR_PORT"
ENV_TOKEN = "NOX_SUPERVISOR_TOKEN"  # noqa: S105 - env var name, not a secret
ENV_SAFE_MODE = "NOX_SAFE_MODE"

MAX_LINE = 64 * 1024


class ProtocolError(ValueError):
    pass


def make(
    name: str,
    payload: dict[str, Any],
    src: Source,
    *,
    kind: Kind = Kind.EVENT,
    corr: str | None = None,
) -> Envelope:
    return Envelope(kind=kind, name=name, payload=payload, src=src, corr=corr)


def auth_frame(token: str, *, role: str, pid: int, src: Source) -> Envelope:
    """The one frame per connection that carries a token (B-1)."""
    return make(NAME_AUTH, {"token": token, "role": role, "pid": pid}, src, kind=Kind.REQUEST)


def encode(envelope: Envelope) -> bytes:
    return (envelope.model_dump_json() + "\n").encode("utf-8")


def decode(line: bytes | str) -> Envelope:
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("message must be a JSON object")
    try:
        return Envelope.model_validate(data)
    except ValidationError as exc:
        raise ProtocolError(f"invalid envelope: {exc.error_count()} error(s)") from exc


async def read_envelope(reader: asyncio.StreamReader) -> Envelope | None:
    """Read one line; None on EOF. Raises ProtocolError for malformed or oversized lines."""
    try:
        line = await reader.readline()
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError("line too long") from exc
    except (ConnectionError, asyncio.IncompleteReadError):
        return None
    if not line:
        return None
    if len(line) > MAX_LINE:
        raise ProtocolError("line too long")
    return decode(line)


def token_path(runtime_dir: Path) -> Path:
    return runtime_dir / TOKEN_FILE


def read_token(runtime_dir: Path) -> str:
    token = token_path(runtime_dir).read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise ValueError("supervisor token file is empty or too short")
    return token


def env_settings() -> tuple[str, int, str] | None:
    """(host, port, token) from the environment the supervisor sets for its children, or None."""
    port = os.environ.get(ENV_PORT)
    token = os.environ.get(ENV_TOKEN)
    if not port or not token:
        return None
    return os.environ.get(ENV_HOST, "127.0.0.1"), int(port), token
