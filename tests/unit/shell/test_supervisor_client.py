from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator

import pytest

from nox.shell.supervisor_client import (
    SupervisorUnavailableError,
    build_kill_message,
    build_stop_message,
    send_supervisor_kill,
    send_supervisor_stop,
)


def _read_one(conn: socket.socket) -> tuple[bytes, dict]:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\n", 1)[0]
    return line, json.loads(line)


@pytest.fixture
def fake_supervisor() -> Iterator[tuple[int, list[dict]]]:
    """Requires the B-1 handshake (`sup.auth` -> `sup.auth_ok`) before the real request."""
    received: list[dict] = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5)
    port = srv.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            return
        with conn:
            _, auth = _read_one(conn)
            received.append(auth)
            conn.sendall(
                (
                    json.dumps({"kind": "response", "name": "sup.auth_ok", "payload": {"ok": True}})
                    + "\n"
                ).encode()
            )
            _, msg = _read_one(conn)
            received.append(msg)
            conn.sendall(
                (
                    json.dumps(
                        {
                            "kind": "response",
                            "name": "sup.ack",
                            "corr": msg["id"],
                            "payload": {"ok": True},
                        }
                    )
                    + "\n"
                ).encode()
            )

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    yield port, received
    th.join(timeout=5)
    srv.close()


def test_kill_message_shape() -> None:
    msg = build_kill_message(reason="user", origin="tray")
    assert msg["name"] == "sup.kill" and msg["kind"] == "request" and msg["v"] == 1
    assert msg["src"] == {"role": "shell", "id": "shell"}
    assert msg["payload"] == {"reason": "user", "origin": "tray"}
    assert "token" not in msg["payload"]  # B-1: no token outside the sup.auth frame


def test_stop_message_shape() -> None:
    msg = build_stop_message(reason="user_quit")
    assert msg["name"] == "sup.stop" and msg["kind"] == "request"
    assert msg["payload"] == {"reason": "user_quit"}


def test_send_kill_round_trip(fake_supervisor: tuple[int, list[dict]]) -> None:
    port, received = fake_supervisor
    reply = send_supervisor_kill("127.0.0.1", port, "sup-token", reason="user", origin="tray")
    assert reply["name"] == "sup.ack" and reply["payload"] == {"ok": True}
    assert received[0]["name"] == "sup.auth" and received[0]["payload"]["token"] == "sup-token"
    assert received[1]["payload"] == {"reason": "user", "origin": "tray"}
    assert reply["corr"] == received[1]["id"]


def test_send_stop_round_trip(fake_supervisor: tuple[int, list[dict]]) -> None:
    port, received = fake_supervisor
    reply = send_supervisor_stop("127.0.0.1", port, "sup-token", reason="user_quit")
    assert reply["name"] == "sup.ack" and reply["payload"] == {"ok": True}
    assert received[1]["name"] == "sup.stop" and received[1]["payload"] == {"reason": "user_quit"}


def test_unreachable_supervisor_raises() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(SupervisorUnavailableError):
        send_supervisor_kill("127.0.0.1", port, "t", reason="user", origin="tray", timeout=0.5)
