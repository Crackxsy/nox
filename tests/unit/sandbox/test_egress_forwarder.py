"""The in-sandbox egress launcher, run directly on the host (no bwrap needed, Unix only).

What is checked here is the launcher's own contract: the target gets stdin/stdout and the exit
code passes through; connections to its port reach the one Unix socket and nothing else; many
connections and half-closes are relayed correctly by its single thread.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets: Linux launcher")

FORWARDER = str(Path(__file__).parents[3] / "src" / "nox" / "sandbox" / "_egress_forwarder.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def upstream() -> Iterator[tuple[str, list[bytes]]]:
    """A Unix-socket server that answers each connection with `echo:` + everything it got."""
    folder = tempfile.mkdtemp(prefix="nox-fwd-test-")
    path = str(Path(folder) / "u.sock")
    seen: list[bytes] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined,unused-ignore]
    server.bind(path)
    server.listen(32)

    def handle(conn: socket.socket) -> None:
        with conn:
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
            seen.append(data)
            conn.sendall(b"echo:" + data)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield path, seen
    server.close()


CLIENT = """
import socket, sys, threading
port, n, size = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
out = [None] * n
def one(i):
    s = socket.create_connection(("127.0.0.1", port), 10)
    s.sendall(bytes([65 + i % 26]) * size)
    s.shutdown(socket.SHUT_WR)
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    out[i] = data
threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
[t.start() for t in threads]; [t.join() for t in threads]
ok = all(o == b"echo:" + bytes([65 + i % 26]) * size for i, o in enumerate(out))
print("ok" if ok else "bad", sys.stdin.read().strip())
sys.exit(7)
"""


def test_relays_many_connections_and_passes_stdio_and_exit_code(
    upstream: tuple[str, list[bytes]],
) -> None:
    path, seen = upstream
    port = _free_port()
    done = subprocess.run(  # noqa: S603 - our own launcher and interpreter
        [sys.executable, "-I", "-S", FORWARDER, path, str(port), "--"]
        + [sys.executable, "-I", "-c", CLIENT, str(port), "12", str(300_000)],
        input=b"from-stdin\n",
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.stdout.decode().split() == ["ok", "from-stdin"], done.stderr.decode()
    assert done.returncode == 7
    assert len(seen) == 12 and all(len(d) == 300_000 for d in seen)


def test_a_missing_program_is_exit_127(upstream: tuple[str, list[bytes]]) -> None:
    path, _ = upstream
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", FORWARDER, path, str(_free_port()), "--", "/nonexistent/x"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 127
    assert b"cannot start" in done.stderr


def test_a_dead_upstream_just_closes_the_connection() -> None:
    port = _free_port()
    code = (
        "import socket, sys\n"
        f"s = socket.create_connection(('127.0.0.1', {port}), 5)\n"
        "try:\n"
        "    s.sendall(b'x')\n"
        "    print(repr(s.recv(10)))\n"
        "except ConnectionResetError:\n"
        "    print(repr(b''))\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", FORWARDER, "/nonexistent/sock", str(port), "--"]
        + [sys.executable, "-I", "-c", code],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.strip() == b"b''"


def test_bad_usage_is_refused() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", FORWARDER, "only-one-arg"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 2
