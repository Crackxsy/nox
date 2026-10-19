"""The Nox loopback egress proxy: the one hole the sandbox's network may pass through.

A sandboxed process has no network at the OS level (Windows low-integrity/AppContainer socket
denial, Linux `--unshare-net`). When a run declares egress, the backend instead gives it a network
path that can reach *only* this proxy, and sets `HTTPS_PROXY`/`HTTP_PROXY` to it. So every
connection the process makes arrives here as an HTTP `CONNECT`, and this is where the policy is
applied - outside the sandbox, in the core's own trust domain.

For each `CONNECT host:port` the proxy:

1. checks `host:port` against the run's declared allow-list (nothing declared ⇒ nothing passes),
2. resolves the host and runs every address through `NetGuard` (loopback, LAN, link-local,
   metadata and the Home Assistant host are refused, which also defeats DNS rebinding),
3. opens the upstream connection to a vetted address and tunnels bytes both ways.

The sandbox's proxy speaks only `CONNECT`; a sandboxed HTTP client is configured to tunnel even
plain HTTP through it, so there is no second, un-vetted forwarding path. A browser cannot be told
that - it sends a plain `http://` URL as `GET http://host/path` to its proxy - so the browser's
proxy is built with `forward_plain_http=True`: such a request is authorized exactly like a
`CONNECT` (same declaration, same `NetGuard` check on the resolved address), sent upstream in
origin form with `Connection: close`, and the connection ends with that one exchange, so a second
request on a kept-alive connection can never reach a host that was not checked. Each decision is
handed to an audit callback. The listener binds `127.0.0.1` on an ephemeral port and hands
callers that port. Name resolution runs in a worker thread, never on the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from nox.core.logging import get_logger
from nox.sandbox.netguard import BlockedAddress, NetGuard, resolve_all

if TYPE_CHECKING:
    from nox.sandbox._winpipe import PipeListener

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
StreamPair = tuple[asyncio.StreamReader, asyncio.StreamWriter]

log = get_logger(__name__)

__all__ = ["EgressProxy", "ProxyDecision", "allow_entry_matches"]

_CRLF = b"\r\n"
#: Request headers that belong to the hop between client and proxy, never forwarded upstream.
_HOP_HEADERS = frozenset(
    {
        b"proxy-connection",
        b"proxy-authorization",
        b"connection",
        b"keep-alive",
        b"te",
        b"trailer",
        b"upgrade",
    }
)
_MAX_HEADER_LINES = 200


def allow_entry_matches(entry: str, host: str, port: int) -> bool:
    """One declared allow-list entry (`host` or `host:port`, `*` wildcards in the host)."""
    from nox.security.egress import entry_matches

    return entry_matches(entry, host.strip().lower().rstrip("."), port)


@dataclass(frozen=True, slots=True)
class ProxyDecision:
    allowed: bool
    host: str
    port: int
    reason: str


AuditFn = Callable[[ProxyDecision], None]
Resolver = Callable[[str, int], list[_IpAddress]]


class EgressProxy:
    """A CONNECT-only proxy bound to loopback that enforces one run's egress declaration."""

    def __init__(
        self,
        *,
        allow: Sequence[str],
        guard: NetGuard,
        audit: AuditFn | None = None,
        resolver: Resolver = resolve_all,
        open_connection: Callable[[str, int], Awaitable[StreamPair]] | None = None,
        forward_plain_http: bool = False,
    ) -> None:
        self._allow = tuple(allow)
        self._forward_plain = forward_plain_http
        self._guard = guard
        self._audit = audit
        self._resolver = resolver
        self._open = open_connection or self._open_upstream
        self._server: asyncio.AbstractServer | None = None
        self._pipe: PipeListener | None = None
        self.port = 0
        #: Where a child reaches the proxy when it is not loopback TCP: a Windows named pipe path
        #: (`start_pipe`). Empty while the proxy only listens on TCP.
        self.socket_path = ""

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        log.info("sandbox.proxy_started", port=self.port, allow=len(self._allow))
        return self.port

    async def start_pipe(self, container_sid: str) -> str:
        """Also listen on a fresh named pipe only `container_sid` (and the core) may open.

        For the Windows AppContainer backend, whose child may not be able to reach a loopback TCP
        port of the host. Connections arriving here get exactly the same `CONNECT` handling and
        policy as TCP ones. Returns the pipe path, also kept in `socket_path`.
        """
        from nox.sandbox._winpipe import PipeListener, pipe_sddl

        listener = PipeListener(sddl=pipe_sddl(container_sid), handler=self._handle)
        self.socket_path = await listener.start()
        self._pipe = listener
        return self.socket_path

    async def stop(self) -> None:
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # -- policy --------------------------------------------------------------------------------

    def authorize(self, host: str, port: int) -> ProxyDecision:
        """The decision for one `CONNECT host:port`, without opening anything."""
        host_l = host.strip().lower().rstrip(".")
        if not any(allow_entry_matches(entry, host_l, port) for entry in self._allow):
            return ProxyDecision(False, host_l, port, "not in the run's egress declaration")
        try:
            addresses = self._resolver(host_l, port)
            self._guard.check_addresses(host_l, addresses)
        except BlockedAddress as blocked:
            return ProxyDecision(False, host_l, port, blocked.reason)
        except OSError as exc:
            return ProxyDecision(False, host_l, port, f"resolution failed: {exc}")
        return ProxyDecision(True, host_l, port, "allowed by declaration and address policy")

    # -- connection handling -------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=10.0)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await _close(writer)
            return
        parts = request.decode("latin-1").strip().split()
        if len(parts) == 3 and self._forward_plain and parts[0].upper() != "CONNECT":
            await self._forward(parts[0], parts[1], parts[2], reader, writer)
            return
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            await _respond(writer, 405, "Method Not Allowed")
            return
        host, _, port_s = parts[1].rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            await _respond(writer, 400, "Bad Request")
            return
        # Drain the rest of the request headers.
        with contextlib.suppress(Exception):
            while True:
                line = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=5.0)
                if line == _CRLF:
                    break

        decision = await asyncio.to_thread(self.authorize, host, port)
        if self._audit is not None:
            self._audit(decision)
        if not decision.allowed:
            log.warning("sandbox.proxy_denied", host=host, port=port, reason=decision.reason)
            await _respond(writer, 403, "Forbidden")
            return

        try:
            up_reader, up_writer = await asyncio.wait_for(self._open(host, port), timeout=10.0)
        except (OSError, TimeoutError) as exc:
            log.warning("sandbox.proxy_upstream_failed", host=host, port=port, error=str(exc))
            await _respond(writer, 502, "Bad Gateway")
            return
        # Not `_respond`: that closes the connection, which would end the tunnel before it began.
        try:
            writer.write(b"HTTP/1.1 200 Connection Established" + _CRLF + _CRLF)
            await writer.drain()
        except (OSError, ConnectionError):
            await _close(writer)
            await _close(up_writer)
            return
        await _tunnel(reader, writer, up_reader, up_writer)

    async def _forward(
        self,
        method: str,
        target: str,
        version: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One plain-HTTP request in absolute form (`GET http://host/path`), vetted and relayed."""
        url = urlsplit(target)
        if url.scheme.lower() != "http" or not url.hostname:
            await _respond(writer, 400, "Bad Request")
            return
        try:
            port = url.port or 80
        except ValueError:
            await _respond(writer, 400, "Bad Request")
            return
        headers: list[bytes] = []
        try:
            while True:
                line = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=5.0)
                if line == _CRLF:
                    break
                headers.append(line)
                if len(headers) > _MAX_HEADER_LINES:
                    raise ValueError("too many header lines")
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            await _respond(writer, 400, "Bad Request")
            return
        host = url.hostname
        decision = await asyncio.to_thread(self.authorize, host, port)
        if self._audit is not None:
            self._audit(decision)
        if not decision.allowed:
            log.warning("sandbox.proxy_denied", host=host, port=port, reason=decision.reason)
            await _respond(writer, 403, "Forbidden")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(self._open(host, port), timeout=10.0)
        except (OSError, TimeoutError) as exc:
            log.warning("sandbox.proxy_upstream_failed", host=host, port=port, error=str(exc))
            await _respond(writer, 502, "Bad Gateway")
            return
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        kept = [h for h in headers if h.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS]
        head = f"{method} {path} {version}\r\n".encode("latin-1")
        head += b"".join(kept) + b"Connection: close\r\n\r\n"
        try:
            up_writer.write(head)
            await up_writer.drain()
        except OSError:
            await _close(up_writer)
            await _respond(writer, 502, "Bad Gateway")
            return
        await _tunnel(reader, writer, up_reader, up_writer)

    async def _open_upstream(self, host: str, port: int) -> StreamPair:
        # Dial a vetted address, never the name: the resolution that is judged here is the one
        # that is dialed, so a rebind between check and connect has nothing to race.
        addresses = await asyncio.to_thread(self._resolver, host, port)
        self._guard.check_addresses(host, addresses)
        return await asyncio.open_connection(str(addresses[0]), port)


async def _respond(writer: asyncio.StreamWriter, code: int, text: str) -> None:
    with contextlib.suppress(Exception):
        writer.write(f"HTTP/1.1 {code} {text}\r\n\r\n".encode("latin-1"))
        await writer.drain()
    await _close(writer)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


async def _tunnel(
    c_reader: asyncio.StreamReader,
    c_writer: asyncio.StreamWriter,
    u_reader: asyncio.StreamReader,
    u_writer: asyncio.StreamWriter,
) -> None:
    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        with contextlib.suppress(Exception):
            dst.write_eof()

    await asyncio.gather(pipe(c_reader, u_writer), pipe(u_reader, c_writer))
    await _close(c_writer)
    await _close(u_writer)
