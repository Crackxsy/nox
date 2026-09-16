"""Second kill-switch path: newline-delimited JSON to the supervisor control port (Process Model).

Used only when the core is unreachable; otherwise `security.kill` goes over the IPC hub. Also the
only path for "Quit" (B-6): the shell asks the supervisor to `sup.stop`, which stops the core
gracefully, then the shell process, then itself. The control channel authenticates once per
connection (B-1): `sup.auth {token, role, pid}` -> `sup.auth_ok`; the actual request that follows
carries no token.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import UTC, datetime
from typing import Any


class SupervisorUnavailableError(RuntimeError):
    """The supervisor control port did not accept the message."""


def _envelope(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "kind": "request",
        "name": name,
        "corr": None,
        "src": {"role": "shell", "id": "shell"},
        "payload": payload,
    }


def build_auth_message(token: str, *, pid: int | None = None) -> dict[str, Any]:
    """`sup.auth` frame (B-1): the only message on this channel that carries the token."""
    return _envelope("sup.auth", {"token": token, "role": "shell", "pid": pid or os.getpid()})


def build_kill_message(*, reason: str, origin: str) -> dict[str, Any]:
    """Envelope-shaped `sup.kill` request, sent after the handshake - no token in the payload."""
    return _envelope("sup.kill", {"reason": reason, "origin": origin})


def build_stop_message(*, reason: str) -> dict[str, Any]:
    """Envelope-shaped `sup.stop` request (B-6): the supervisor stops core, shell and itself."""
    return _envelope("sup.stop", {"reason": reason})


def _send_after_handshake(
    host: str, port: int, token: str, request: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    """Authenticate once (B-1), then send `request` and return the first reply line."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((json.dumps(build_auth_message(token)) + "\n").encode("utf-8"))
            auth_line = _read_line(sock, timeout)
            if not auth_line:
                raise SupervisorUnavailableError(
                    "supervisor closed the connection during handshake"
                )
            try:
                auth_reply = json.loads(auth_line)
            except ValueError as exc:
                raise SupervisorUnavailableError(
                    "supervisor answered the handshake with invalid JSON"
                ) from exc
            if not isinstance(auth_reply, dict) or auth_reply.get("name") != "sup.auth_ok":
                raise SupervisorUnavailableError(f"supervisor auth failed: {auth_reply}")
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
            reply = _read_line(sock, timeout)
    except OSError as exc:
        msg = f"supervisor control port {host}:{port} unreachable"
        raise SupervisorUnavailableError(msg) from exc
    if not reply:
        return {}
    try:
        parsed = json.loads(reply)
    except ValueError as exc:
        raise SupervisorUnavailableError("supervisor answered with invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {}


def send_supervisor_kill(
    host: str, port: int, token: str, *, reason: str, origin: str, timeout: float = 2.0
) -> dict[str, Any]:
    """Authenticate, then send `sup.kill` and return the first JSON line the supervisor answers."""
    return _send_after_handshake(
        host, port, token, build_kill_message(reason=reason, origin=origin), timeout=timeout
    )


def send_supervisor_stop(
    host: str, port: int, token: str, *, reason: str, timeout: float = 2.0
) -> dict[str, Any]:
    """Authenticate, then send `sup.stop` (B-6): the supervisor stops core, shell and itself."""
    return _send_after_handshake(
        host, port, token, build_stop_message(reason=reason), timeout=timeout
    )


def _read_line(sock: socket.socket, timeout: float) -> str:
    sock.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf and len(buf) < 65536:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
