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

The proxy speaks only `CONNECT`; a sandboxed HTTP client is configured to tunnel even plain HTTP
through it, so there is no second, un-vetted forwarding path. Each decision is handed to an audit
callback. The listener binds `127.0.0.1` on an ephemeral port and hands callers that port.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from nox.core.logging import get_logger
from nox.sandbox.netguard import BlockedAddress, NetGuard, resolve_all

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
StreamPair = tuple[asyncio.StreamReader, asyncio.StreamWriter]

log = get_logger(__name__)

__all__ = ["EgressProxy", "ProxyDecision", "allow_entry_matches"]

_CRLF = b"\r\n"


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
    ) -> None:
        self._allow = tuple(allow)
        self._guard = guard
        self._audit = audit
        self._resolver = resolver
        self._open = open_connection or self._open_upstream
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        log.info("sandbox.proxy_started", port=self.port, allow=len(self._allow))
        return self.port

    async def stop(self) -> None:
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

        decision = self.authorize(host, port)
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
        await _respond(writer, 200, "Connection Established")
        await _tunnel(reader, writer, up_reader, up_writer)

    async def _open_upstream(self, host: str, port: int) -> StreamPair:
        # Connect to a vetted address, not the name again: authorize() already resolved and judged
        # it, and reconnecting by name would open a second resolution a rebind could race.
        addresses = self._resolver(host, port)
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
