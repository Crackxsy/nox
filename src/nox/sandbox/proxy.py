"""The Nox loopback egress proxy: the one hole the sandbox's network may pass through.

A sandboxed process has no network at the OS level (Windows low-integrity/AppContainer socket
denial, Linux `--unshare-net`). When a run declares egress, the backend instead gives it a network
path that can reach *only* this proxy, and sets `HTTPS_PROXY`/`HTTP_PROXY` to it. So every
connection the process makes arrives here as an HTTP `CONNECT`, and this is where the policy is
applied - outside the sandbox, in the core's own trust domain.

For each `CONNECT host:port` the proxy:

1. checks the per-proxy credentials (below),
2. checks `host:port` against the run's declared allow-list (nothing declared ⇒ nothing passes),
3. resolves the host and runs every address through `NetGuard` (loopback, LAN, link-local,
   metadata and the Home Assistant host are refused, which also defeats DNS rebinding),
4. opens the upstream connection to the vetted address and tunnels bytes both ways.

**Credentials.** Loopback is reachable by every local process, so a proxy without a secret would
be an open egress path past the policy for any program on the machine - another run's sandboxed
child included. Each proxy may therefore carry its own random credentials
(`EgressProxy.new_credentials()`): every request must present them as `Proxy-Authorization:
Basic`, compared in constant time, or gets `407` and a closed connection. The sandbox puts them
into the child's proxy URL (`http://user:secret@127.0.0.1:port`), so a child can use the proxy of
*its own* run and no other; the browser hands them to Chromium through Playwright. A proxy lives
exactly as long as the run, process or browser session it was made for.

**Plain HTTP.** The sandbox's proxy speaks only `CONNECT`; a sandboxed HTTP client tunnels even
plain HTTP through it. A browser cannot be told that - it sends `GET http://host/path` to its
proxy - so the browser's proxy is built with `forward_plain_http=True`. Such a connection carries
exactly **one** exchange: the request is read completely (`Content-Length`, or `chunked` decoded;
both at once, or any other transfer coding, is refused as a smuggling attempt), authorized like a
`CONNECT`, sent upstream in origin form with `Connection: close`, and exactly one response is
relayed back with `Connection: close` - then both sides are closed. Nothing the client sends after
its first request is ever read, so a pipelined second request to another host goes nowhere.

Each decision is handed to an audit callback. The listener binds `127.0.0.1` on an ephemeral port,
or - for the Linux sandbox, whose child has no route to the host's loopback - a Unix domain socket
in a private directory (`start_unix`).
Name resolution runs in a worker thread, never on the event loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import os
import secrets
import shutil
import sys
import tempfile
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
        b"content-length",
        b"transfer-encoding",
    }
)
_RESPONSE_HOP_HEADERS = frozenset({b"connection", b"keep-alive", b"proxy-connection"})
_MAX_HEADER_LINES = 200
#: Largest request body the browser proxy forwards (a form post, an upload).
MAX_BODY_BYTES = 32 * 1024 * 1024
_IDLE_S = 60.0


class _BadRequestError(ValueError):
    pass


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
    """A loopback proxy that enforces one run's egress declaration (see the module docstring)."""

    def __init__(
        self,
        *,
        allow: Sequence[str],
        guard: NetGuard,
        audit: AuditFn | None = None,
        resolver: Resolver = resolve_all,
        open_connection: Callable[[str, int], Awaitable[StreamPair]] | None = None,
        forward_plain_http: bool = False,
        credentials: tuple[str, str] | None = None,
    ) -> None:
        self._allow = tuple(allow)
        self._guard = guard
        self._audit = audit
        self._resolver = resolver
        self._open = open_connection or self._open_upstream
        self._forward_plain = forward_plain_http
        self.credentials = credentials
        self._expected_auth = (
            base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()) if credentials else None
        )
        self._server: asyncio.AbstractServer | None = None
        self._pipe: PipeListener | None = None
        self.port = 0
        #: Where a child reaches the proxy when it is not loopback TCP: a Windows named pipe path
        #: (`start_pipe`) or a Unix socket path (`start_unix`). Empty while it only listens on TCP.
        self.socket_path = ""
        #: The private folder holding the Unix socket; removed by `stop()`. Never the pipe path.
        self._unix_dir = ""

    @staticmethod
    def new_credentials() -> tuple[str, str]:
        """Fresh random credentials for one proxy's lifetime."""
        return "nox", secrets.token_urlsafe(32)

    @property
    def url(self) -> str:
        """The proxy URL a client is given, credentials included when the proxy has them."""
        userinfo = f"{self.credentials[0]}:{self.credentials[1]}@" if self.credentials else ""
        return f"http://{userinfo}127.0.0.1:{self.port}"

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

    async def start_unix(self) -> str:
        """Listen on a Unix domain socket instead of loopback TCP; returns its path.

        The Linux sandbox binds this one file into a process whose network namespace has no route
        anywhere (see `nox.sandbox.linux`). The socket sits in a fresh `0700` directory of the
        core's user, so nothing but that user - and the one sandbox it is bound into - can reach
        it; the per-proxy credentials still apply on top. `stop()` removes it.
        """
        if sys.platform == "win32":
            raise OSError("the Unix-socket egress proxy exists only for the Linux sandbox")
        folder = tempfile.mkdtemp(prefix="nox-egress-")
        path = os.path.join(folder, "egress.sock")
        try:
            self._server = await asyncio.start_unix_server(self._handle, path)
            os.chmod(path, 0o600)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.socket_path = path
        self._unix_dir = folder
        log.info("sandbox.proxy_started", transport="unix", allow=len(self._allow))
        return path

    async def stop(self) -> None:
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._unix_dir:
            shutil.rmtree(self._unix_dir, ignore_errors=True)
            self._unix_dir = ""
        self.socket_path = ""

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

    def authenticated(self, headers: Sequence[bytes]) -> bool:
        """Whether the request carries this proxy's credentials (always true without any)."""
        if self._expected_auth is None:
            return True
        for line in headers:
            name, _, value = line.partition(b":")
            if name.strip().lower() != b"proxy-authorization":
                continue
            scheme, _, token = value.strip().partition(b" ")
            if scheme.lower() == b"basic" and hmac.compare_digest(
                token.strip(), self._expected_auth
            ):
                return True
        return False

    # -- connection handling -------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=10.0)
            headers = await _read_headers(reader)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            _BadRequestError,
        ):
            await _close(writer)
            return
        parts = request.decode("latin-1").strip().split()
        if len(parts) != 3:
            await _respond(writer, 400, "Bad Request")
            return
        if not self.authenticated(headers):
            log.warning("sandbox.proxy_unauthenticated")
            await _respond(
                writer,
                407,
                "Proxy Authentication Required",
                b'Proxy-Authenticate: Basic realm="nox"\r\n',
            )
            return
        method, target, version = parts
        if method.upper() == "CONNECT":
            await self._connect(target, reader, writer)
        elif self._forward_plain:
            await self._forward(method, target, version, headers, reader, writer)
        else:
            await _respond(writer, 405, "Method Not Allowed")

    async def _decide(self, host: str, port: int) -> ProxyDecision:
        decision = await asyncio.to_thread(self.authorize, host, port)
        if self._audit is not None:
            self._audit(decision)
        if not decision.allowed:
            log.warning("sandbox.proxy_denied", host=host, port=port, reason=decision.reason)
        return decision

    async def _dial(self, host: str, port: int, writer: asyncio.StreamWriter) -> StreamPair | None:
        try:
            return await asyncio.wait_for(self._open(host, port), timeout=10.0)
        except (OSError, TimeoutError) as exc:
            log.warning("sandbox.proxy_upstream_failed", host=host, port=port, error=str(exc))
            await _respond(writer, 502, "Bad Gateway")
            return None

    async def _connect(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
        except ValueError:
            await _respond(writer, 400, "Bad Request")
            return
        if not (await self._decide(host, port)).allowed:
            await _respond(writer, 403, "Forbidden")
            return
        upstream = await self._dial(host, port, writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        # A failed write of the 200 must not leave the upstream connection dangling.
        try:
            await _respond_open(writer, 200, "Connection Established")
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
        headers: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Exactly one plain-HTTP exchange in absolute form (`GET http://host/path`)."""
        url = urlsplit(target)
        try:
            if url.scheme.lower() != "http" or not url.hostname:
                raise _BadRequestError("not an absolute http URL")
            port = url.port or 80
            body = await _read_body(headers, reader)
        except (ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await _respond(writer, 400, "Bad Request")
            return
        host = url.hostname
        if not (await self._decide(host, port)).allowed:
            await _respond(writer, 403, "Forbidden")
            return
        upstream = await self._dial(host, port, writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        kept = [h for h in headers if h.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS]
        head = f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(kept)
        if body or method.upper() in ("POST", "PUT", "PATCH"):
            head += f"Content-Length: {len(body)}\r\n".encode("latin-1")
        head += b"Connection: close\r\n\r\n"
        try:
            up_writer.write(head + body)
            await up_writer.drain()
            await _relay_one_response(up_reader, writer, head_only=method.upper() == "HEAD")
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            with contextlib.suppress(Exception):
                await _respond(writer, 502, "Bad Gateway")
        finally:
            await _close(up_writer)
            await _close(writer)

    async def _open_upstream(self, host: str, port: int) -> StreamPair:
        # Dial a vetted address, never the name: the resolution that is judged here is the one
        # that is dialed, so a rebind between check and connect has nothing to race.
        addresses = await asyncio.to_thread(self._resolver, host, port)
        self._guard.check_addresses(host, addresses)
        return await asyncio.open_connection(str(addresses[0]), port)


async def _read_headers(reader: asyncio.StreamReader) -> list[bytes]:
    headers: list[bytes] = []
    while True:
        line = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=10.0)
        if line == _CRLF:
            return headers
        headers.append(line)
        if len(headers) > _MAX_HEADER_LINES:
            raise _BadRequestError("too many header lines")


def _header(headers: Sequence[bytes], name: bytes) -> list[bytes]:
    return [
        line.partition(b":")[2].strip()
        for line in headers
        if line.partition(b":")[0].strip().lower() == name
    ]


async def _read_body(headers: Sequence[bytes], reader: asyncio.StreamReader) -> bytes:
    """The complete request body - and nothing after it. Ambiguous framing is refused."""
    lengths = _header(headers, b"content-length")
    codings = _header(headers, b"transfer-encoding")
    if codings and lengths:
        raise _BadRequestError("both Content-Length and Transfer-Encoding")
    if codings:
        if len(codings) != 1 or codings[0].lower() != b"chunked":
            raise _BadRequestError("unsupported transfer coding")
        return await _read_chunked(reader)
    if not lengths:
        return b""
    if len(set(lengths)) != 1 or not lengths[0].isdigit():
        raise _BadRequestError("bad Content-Length")
    size = int(lengths[0])
    if size > MAX_BODY_BYTES:
        raise _BadRequestError("request body too large")
    return await asyncio.wait_for(reader.readexactly(size), timeout=_IDLE_S)


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        line = await asyncio.wait_for(reader.readuntil(_CRLF), timeout=_IDLE_S)
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise _BadRequestError("bad chunk size") from exc
        if size == 0:
            await _read_headers(reader)  # trailers, discarded
            return bytes(body)
        if len(body) + size > MAX_BODY_BYTES:
            raise _BadRequestError("request body too large")
        body += await asyncio.wait_for(reader.readexactly(size), timeout=_IDLE_S)
        if await reader.readexactly(2) != _CRLF:
            raise _BadRequestError("bad chunk terminator")


async def _relay_one_response(
    up_reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, head_only: bool
) -> None:
    """Relay one response, rewritten to `Connection: close`, then stop."""
    status = await asyncio.wait_for(up_reader.readuntil(_CRLF), timeout=_IDLE_S)
    headers = await _read_headers(up_reader)
    kept = [h for h in headers if h.partition(b":")[0].strip().lower() not in _RESPONSE_HOP_HEADERS]
    writer.write(status + b"".join(kept) + b"Connection: close\r\n\r\n")
    await writer.drain()
    if head_only:
        return
    lengths = _header(headers, b"content-length")
    chunked = any(c.lower().endswith(b"chunked") for c in _header(headers, b"transfer-encoding"))
    remaining = int(lengths[0]) if lengths and lengths[0].isdigit() and not chunked else None
    while remaining is None or remaining > 0:
        want = 65536 if remaining is None else min(65536, remaining)
        chunk = await asyncio.wait_for(up_reader.read(want), timeout=_IDLE_S)
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()
        if remaining is not None:
            remaining -= len(chunk)


async def _respond(writer: asyncio.StreamWriter, code: int, text: str, extra: bytes = b"") -> None:
    with contextlib.suppress(Exception):
        writer.write(
            f"HTTP/1.1 {code} {text}\r\n".encode("latin-1")
            + extra
            + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
    await _close(writer)


async def _respond_open(writer: asyncio.StreamWriter, code: int, text: str) -> None:
    writer.write(f"HTTP/1.1 {code} {text}\r\n\r\n".encode("latin-1"))
    await writer.drain()


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
    """Bytes both ways - only ever for a vetted `CONNECT` (an opaque, usually TLS, stream)."""

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
