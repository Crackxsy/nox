"""In-sandbox launcher for the Linux backend: a loopback port that leads only to the egress proxy.

A process under `bwrap --unshare-net` has a network namespace of its own with nothing but its own
`lo` in it - no route to the host, the LAN or the host's loopback. Nox's egress proxy listens on a
Unix domain socket instead, which `bwrap` binds into the sandbox (a file, not a network path). This
script runs *inside* the sandbox, before the target:

1. it listens on `127.0.0.1:<port>` - the sandbox's own loopback, invisible to anything outside;
2. it starts the target with the inherited stdio and environment (`HTTPS_PROXY` already points at
   that port, credentials included - they never appear on a command line);
3. every connection to the port is relayed byte for byte to the one Unix socket path it was given,
   and nowhere else. It never parses, resolves or dials anything: all policy stays in the proxy,
   outside the sandbox, which checks the credentials, the run's declaration and `NetGuard`.

It exits with the target's exit code (128 + N when the target died of signal N), which ends the
sandbox. Standard library only, and kept compatible with Python 3.8+, because it runs on the
system interpreter of whatever Linux the server is on (Raspberry Pi OS, Debian, Ubuntu).

Usage: python3 -I -S _egress_forwarder.py <unix-socket-path> <port> -- <argv...>
"""

from __future__ import annotations

import os
import selectors
import signal
import socket
import subprocess
import sys
import threading

_CHUNK = 64 * 1024
#: Bytes buffered per direction before the relay stops reading that side (back-pressure).
_HIGH_WATER = 256 * 1024


class _Side:
    """One socket of a relayed pair and the bytes waiting to be written to it."""

    __slots__ = ("eof", "out", "peer", "shut", "sock")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.out = bytearray()
        self.eof = False  # this side has sent everything it will send
        self.shut = False  # the write half towards this side is closed
        self.peer: _Side | None = None


class Relay:
    """A single-threaded relay from a listening TCP socket to one fixed Unix socket path.

    One thread for any number of connections: the sandbox's process ceiling (`TasksMax`) counts
    threads, so a thread per connection would let a busy client starve its own target.
    """

    def __init__(self, listener: socket.socket, unix_path: str) -> None:
        self._listener = listener
        self._path = unix_path
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._stop = False
        listener.setblocking(False)
        self._wake_r.setblocking(False)
        self._sel.register(listener, selectors.EVENT_READ, None)
        self._sel.register(self._wake_r, selectors.EVENT_READ, None)

    def stop(self) -> None:
        self._stop = True
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def serve(self) -> None:
        while not self._stop:
            for key, mask in self._sel.select():
                side = key.data
                if side is None:
                    if key.fileobj is self._listener:
                        self._accept()
                    continue
                if mask & selectors.EVENT_READ:
                    self._read(side)
                if mask & selectors.EVENT_WRITE:
                    self._write(side)
        for key in list(self._sel.get_map().values()):
            try:
                key.fileobj.close()  # type: ignore[union-attr]
            except OSError:
                pass

    def _accept(self) -> None:
        if sys.platform == "win32":  # never: this only runs inside the Linux sandbox
            return
        try:
            client, _ = self._listener.accept()
        except OSError:
            return
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self._path)
        except OSError:
            client.close()
            upstream.close()
            return
        a, b = _Side(client), _Side(upstream)
        a.peer, b.peer = b, a
        for side in (a, b):
            side.sock.setblocking(False)
            self._sel.register(side.sock, selectors.EVENT_READ, side)

    def _read(self, side: _Side) -> None:
        peer = side.peer
        if peer is None or side.eof:
            return
        try:
            data = side.sock.recv(_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(side)
            return
        if data:
            peer.out += data
        else:
            side.eof = True
        self._update(side)
        self._update(peer)

    def _write(self, side: _Side) -> None:
        if side.out:
            try:
                sent = side.sock.send(side.out)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._drop(side)
                return
            del side.out[:sent]
        peer = side.peer
        if peer is not None:
            self._update(side)
            self._update(peer)

    def _update(self, side: _Side) -> None:
        """Recompute what to wait for on `side`, half-close or close the pair when done."""
        peer = side.peer
        if peer is None:
            return
        if not side.out and peer.eof and not side.shut:
            side.shut = True
            try:
                side.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        if side.shut and peer.shut:
            self._drop(side)
            return
        events = 0
        if not side.eof and len(peer.out) < _HIGH_WATER:
            events |= selectors.EVENT_READ
        if side.out:
            events |= selectors.EVENT_WRITE
        try:
            registered = self._sel.get_key(side.sock) is not None
        except (KeyError, ValueError):
            registered = False
        try:
            if events and registered:
                self._sel.modify(side.sock, events, side)
            elif events:
                self._sel.register(side.sock, events, side)
            elif registered:
                self._sel.unregister(side.sock)
        except (KeyError, ValueError):
            pass

    def _drop(self, side: _Side) -> None:
        for s in (side, side.peer):
            if s is None:
                continue
            s.peer = None
            try:
                self._sel.unregister(s.sock)
            except (KeyError, ValueError):
                pass
            try:
                s.sock.close()
            except OSError:
                pass


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        sys.stderr.write("usage: _egress_forwarder.py <unix-socket> <port> -- <argv...>\n")
        return 2
    unix_path, port_text, command = argv[0], argv[1], argv[3:]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(port_text)))
    listener.listen(64)
    relay = Relay(listener, unix_path)
    thread = threading.Thread(target=relay.serve, name="egress-relay", daemon=True)
    thread.start()
    try:
        child = subprocess.Popen(command)  # inherits stdio and the environment; no shell
    except OSError as exc:
        sys.stderr.write(f"nox sandbox: cannot start {command[0]}: {exc}\n")
        return 127

    def forward(signum: int, _frame: object) -> None:
        try:
            child.send_signal(signum)
        except OSError:
            pass

    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, forward)
    code = child.wait()
    relay.stop()
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    _code = main(sys.argv[1:])
    sys.stderr.flush()
    os._exit(_code)  # without waiting for the relay thread's open connections
