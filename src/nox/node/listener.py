"""The node listener: where `nox node`s connect, on its own port (`ipc.lan.node_port`, 47844).

Separate from the browser listener on 47843 on purpose: the dashboard's hardening (Origin, Host,
CSP) stays untouched, and a firewall rule for "nodes" stays readable. What a connection must pass,
in order, before a single application message is read:

1. **TLS 1.3 with a client certificate** signed by this installation's authority
   (`nox.lan.tls.node_server_context`). No certificate, another authority, TLS 1.2: the handshake
   fails.
2. **The certificate belongs to a paired node** (`NodePairing.identify`): a node certificate (EKU
   clientAuth, `urn:nox:node:<id>`), its serial issued to that node and not on the denylist, the
   node's pairing not revoked, the key the one that was paired. This runs right after the
   handshake, before the WebSocket upgrade is answered - the standard library has no verify
   callback inside the handshake, and nothing is read from the peer before this check passes.
3. **No browser:** a request with an `Origin` header is refused; only `NODE_PATH` exists.
4. The node's first message must be `initialize` within `HELLO_TIMEOUT_S`.

After that the core is an MCP client of the node (`McpClient` over `WebSocketTransport`). What the
node may send on its own is the role `node` allow-list, `nox.node.protocol.NODE_TO_CORE`; anything
else is refused, audited, and after `MAX_VIOLATIONS` the connection is closed.

Revoking a node (`NodePairing.revoke`) sends `nox/revoked` and closes its connection at once.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http
import json
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from pydantic import BaseModel, ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from nox.core.logging import get_logger
from nox.lan.tls import TlsMaterial, node_server_context, permitted_address
from nox.mcp.client import McpClient, McpClientError, WebSocketTransport
from nox.node import protocol as p
from nox.node.pairing import NodePairing
from nox.node.repo import NodeRepository
from nox.remote.models import DeviceRow
from nox.security.model import AuditLog

log = get_logger(__name__)

__all__ = ["HELLO_TIMEOUT_S", "MAX_VIOLATIONS", "NodeListener", "NodeSession"]

HELLO_TIMEOUT_S = 5.0
#: Methods outside the allow-list (or malformed parameters) a node may send before it is cut off.
MAX_VIOLATIONS = 5
#: The core pings every node this often; a node that answered nothing for `ALIVE_WINDOW_S` counts
#: as gone for routing even before its socket closes.
PING_INTERVAL_S = 1.0
ALIVE_WINDOW_S = 3.0
#: TLS handshake budget per connection: a peer that starts one and stalls is dropped then.
HANDSHAKE_TIMEOUT_S = 5.0
#: Connections between the upgrade and the node's `initialize`: per node (a newer one
#: displaces the oldest, so one node - or one address behind a NAT - can never lock out another)
#: and in total. Before the certificate is known, the handshake timeout bounds a connection.
MAX_PREAUTH_PER_DEVICE = 2
MAX_PREAUTH_TOTAL = 16
#: Pending TCP connections the operating system queues for the listener.
BACKLOG = 32
#: A node's `security.kill` always stops Nox when it is not stopped yet. While it already is,
#: repeats change nothing and are audited at most once per this interval.
KILL_MIN_INTERVAL_S = 10.0
#: Blob chunks of a result that never completes are dropped after this long.
BLOB_TTL_S = 60.0
#: A node cut off for violations is refused this long, so it cannot flood the audit trail by
#: reconnecting.
VIOLATION_COOLDOWN_S = 30.0
#: Results being assembled from `nox/blob` chunks at one time, per node.
MAX_PENDING_BLOBS = 8
CLOSE_POLICY_VIOLATION = 1008
CLOSE_GOING_AWAY = 1001
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602

SecurityKillFn = Callable[[str, str], Awaitable[None]]
SessionFn = Callable[["NodeSession"], Awaitable[None]]


@dataclass
class NodeSession:
    """One connected node."""

    device: DeviceRow
    hello: p.NodeHello
    client: McpClient
    connection: ServerConnection
    serial: str
    compatible: bool
    transport: WebSocketTransport
    connected_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    violations: int = 0
    #: Set once the node got its `initialize` answer: only then may the core send it requests.
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when the node answered `nox/killed` after the last `nox/kill`.
    killed: asyncio.Event = field(default_factory=asyncio.Event)
    killed_calls: list[str] = field(default_factory=list)
    #: The certificate's end: the session closes when it is reached (review N1).
    cert_not_after: datetime | None = None
    blobs: dict[str, list[bytes]] = field(default_factory=dict)
    complete: set[str] = field(default_factory=set)
    blob_bytes: int = 0
    blob_started: dict[str, float] = field(default_factory=dict)

    @property
    def device_id(self) -> str:
        return self.device.id

    @property
    def alive(self) -> bool:
        return time.monotonic() - self.last_seen <= ALIVE_WINDOW_S

    def take_blob(self, call_id: str) -> bytes | None:
        """The chunks of one result, joined, once its last chunk arrived; None otherwise."""
        if call_id not in self.complete:
            return None
        self.complete.discard(call_id)
        self.blob_started.pop(call_id, None)
        data = b"".join(self.blobs.pop(call_id, []))
        self.blob_bytes -= len(data)
        return data

    async def answer(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(McpClientError, ConnectionClosed):
            await self.transport.send(message)


class NodeListener:
    def __init__(
        self,
        *,
        pairing: NodePairing,
        nodes: NodeRepository,
        tls: Callable[[], TlsMaterial | None],
        host: str,
        port: int,
        audit: AuditLog | None = None,
        on_security_kill: SecurityKillFn | None = None,
        on_connected: SessionFn | None = None,
        on_disconnected: SessionFn | None = None,
        on_tools_changed: SessionFn | None = None,
        receipt_key: str = "",
        ping_interval_s: float = PING_INTERVAL_S,
        allow_public_clients: bool = False,
        kill_engaged: Callable[[], bool] | None = None,
    ) -> None:
        self._pairing = pairing
        self._nodes = nodes
        self._tls = tls
        self._host = host
        self._port = port
        self._audit = audit
        self._on_security_kill = on_security_kill
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_tools_changed = on_tools_changed
        self._receipt_key = receipt_key
        self._ping_interval_s = ping_interval_s
        self._allow_public = allow_public_clients
        self._server: Server | None = None
        # Weak: a connection whose upgrade failed after the check leaves nothing behind.
        self._identified: weakref.WeakKeyDictionary[
            ServerConnection, tuple[DeviceRow, x509.Certificate]
        ] = weakref.WeakKeyDictionary()
        #: Connections past the certificate check that have not said hello yet -> (device, since).
        self._preauth: weakref.WeakKeyDictionary[ServerConnection, tuple[str, float]] = (
            weakref.WeakKeyDictionary()
        )
        self._kill_engaged = kill_engaged
        self._last_kill: dict[str, float] = {}
        #: device id -> monotonic time until which a node cut off for violations is refused.
        self._cooling: dict[str, float] = {}
        #: Per device: replacing/registering a session and revoking it never interleave.
        self._locks: dict[str, asyncio.Lock] = {}
        #: Devices revoked while this listener runs; checked after every await of a new connection.
        self._removed: set[str] = set()
        self.sessions: dict[str, NodeSession] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._ca_fingerprint = ""
        self._server_serial = 0
        self._context: Any = None
        pairing.on_revoked(self.disconnect)

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        for sock in self._server.sockets:
            return int(sock.getsockname()[1])
        return self._port

    @property
    def core_id(self) -> str:
        """The authority's fingerprint: what a node pinned, and how it recognises this core."""
        return self._ca_fingerprint.replace(":", "").lower()

    async def start(self) -> None:
        material = self._tls()
        if material is None:
            raise RuntimeError("no certificates yet")
        self._context = node_server_context(material)
        self._ca_fingerprint = material.fingerprint
        self._server_serial = material.server_serial
        self._server = await serve(
            self._handle,
            self._host,
            self._port,
            ssl=self._context,
            process_request=self._process_request,
            max_size=p.MAX_FRAME_BYTES,
            compression=None,
            ping_interval=None,
            open_timeout=HELLO_TIMEOUT_S,
            server_header=None,
            ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S,
            backlog=BACKLOG,
        )
        log.info("node.listener_ready", port=self.port)

    async def reload(self) -> None:
        """Pick up re-issued certificates. A new server certificate is loaded for the next
        handshakes; a new authority invalidates every node certificate, so all are cut off."""
        material = self._tls()
        if material is None or self._server is None:
            return
        if material.fingerprint != self._ca_fingerprint:
            await self.stop()
            await self.start()
            return
        if material.server_serial != self._server_serial:
            self._context.load_cert_chain(str(material.cert_file), str(material.key_file))
            self._server_serial = material.server_serial

    async def stop(self) -> None:
        for session in list(self.sessions.values()):
            await self._close(session, CLOSE_GOING_AWAY, "core stopping")
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- before the upgrade --------------------------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path != p.NODE_PATH:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
        if "Origin" in request.headers:
            log.warning("node.origin_refused")
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        peer = connection.remote_address[0] if connection.remote_address else ""
        if not self._allow_public and not permitted_address(str(peer).split("%", 1)[0]):
            log.warning("node.peer_refused", peer=str(peer)[:64])
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        ssl_object = connection.transport.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        if not der:
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        try:
            cert = x509.load_der_x509_certificate(der)
        except ValueError:
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        device = self._pairing.identify(cert)
        if device is None:
            log.warning("node.certificate_refused", serial=f"{cert.serial_number:x}")
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        if self._cooling.get(device.id, 0.0) > time.monotonic():
            return connection.respond(http.HTTPStatus.TOO_MANY_REQUESTS, "later\n")
        own = sorted(
            ((since, conn) for conn, (dev, since) in self._preauth.items() if dev == device.id),
            key=lambda item: item[0],
        )
        while len(own) >= MAX_PREAUTH_PER_DEVICE:
            _since, oldest = own.pop(0)
            self._preauth.pop(oldest, None)
            self._identified.pop(oldest, None)
            if oldest.transport is not None:
                oldest.transport.abort()  # the newer attempt of the same node wins
        if len(self._preauth) >= MAX_PREAUTH_TOTAL:
            log.warning("node.preauth_full", device_id=device.id)
            return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "busy\n")
        self._preauth[connection] = (device.id, time.monotonic())
        self._identified[connection] = (device, cert)
        return None

    # -- the connection ------------------------------------------------------------------------

    async def _handle(self, connection: ServerConnection) -> None:
        identified = self._identified.pop(connection, None)
        if identified is None:  # pragma: no cover - process_request always runs first
            await connection.close(CLOSE_POLICY_VIOLATION, "not identified")
            return
        device, cert = identified
        serial = f"{cert.serial_number:x}"
        try:
            hello = await self._hello(connection)
        finally:
            self._preauth.pop(connection, None)
        if hello is None:
            await connection.close(CLOSE_POLICY_VIOLATION, "initialize expected")
            return
        # Revoked while it was saying hello: the check before the upgrade is not enough.
        if self._is_removed(device.id, cert):
            await connection.close(CLOSE_POLICY_VIOLATION, "removed")
            return
        hello_msg, hello_id = hello
        low, high = p.NODE_PROTOCOL_RANGE
        compatible = low <= hello_msg.node_protocol <= high
        session: NodeSession | None = None

        async def on_peer(message: dict[str, Any]) -> None:
            assert session is not None
            await self._peer(session, message)

        transport = WebSocketTransport(
            connection, on_peer=on_peer, max_message_bytes=p.MAX_FRAME_BYTES
        )

        async def tools_changed() -> None:
            if session is not None and self._on_tools_changed is not None:
                await self._on_tools_changed(session)

        client = McpClient(
            transport, client_version=str(p.NODE_PROTOCOL), on_tools_changed=tools_changed
        )
        session = NodeSession(
            device=device,
            hello=hello_msg,
            client=client,
            connection=connection,
            serial=serial,
            compatible=compatible,
            transport=transport,
            cert_not_after=cert.not_valid_after_utc,
        )
        # Replacing an older session and registering this one happen under the device's lock,
        # which a revocation takes too: a revocation either sees this session registered and
        # closes it, or ran before and is caught by the checks after registering.
        async with self._lock(device.id):
            if self._is_removed(device.id, cert):
                await self._close(session, CLOSE_POLICY_VIOLATION, "removed")
                return
            previous = self.sessions.get(device.id)
            if previous is not None:
                await self._close(previous, CLOSE_POLICY_VIOLATION, "replaced by a new connection")
            self.sessions[device.id] = session
            if self._is_removed(device.id, cert):
                await self._close(session, CLOSE_POLICY_VIOLATION, "removed")
                return
        await asyncio.to_thread(
            self._nodes.update_hello,
            device.id,
            version=hello_msg.node_version,
            manifest_hash=hello_msg.manifest_hash,
        )
        if self._is_removed(device.id) or client.closed:
            await self._close(session, CLOSE_POLICY_VIOLATION, "removed")
            return
        await transport.send(
            {
                "jsonrpc": "2.0",
                "id": hello_id,
                "result": {
                    "protocolVersion": hello_msg.protocolVersion,
                    "node_protocol": list(p.NODE_PROTOCOL_RANGE),
                    "compatible": compatible,
                    "core_id": self.core_id,
                    "node_id": device.id,
                    "receipt_key": self._receipt_key,
                    "engaged": bool(self._kill_engaged is not None and self._kill_engaged()),
                    "serverInfo": {"name": "nox-core", "version": str(p.NODE_PROTOCOL)},
                },
            }
        )
        if self._is_removed(device.id) or client.closed:
            await self._close(session, CLOSE_POLICY_VIOLATION, "removed")
            return
        self._append_audit("node.connect", device.id, "allow", "ok" if compatible else "outdated")
        log.info("node.connected", device_id=device.id, compatible=compatible)
        client.start()
        session.ready.set()
        pinger = asyncio.create_task(self._ping(session), name=f"nox-node-ping-{device.id[:8]}")
        try:
            if self._on_connected is not None:
                await self._on_connected(session)
            await client.wait_closed()
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pinger
            if self.sessions.get(device.id) is session:
                del self.sessions[device.id]
            await client.close()
            log.info("node.disconnected", device_id=device.id)
            if self._on_disconnected is not None:
                with contextlib.suppress(Exception):
                    await self._on_disconnected(session)

    async def _hello(self, connection: ServerConnection) -> tuple[p.NodeHello, Any] | None:
        try:
            frame = await asyncio.wait_for(connection.recv(), HELLO_TIMEOUT_S)
            message = json.loads(frame) if isinstance(frame, str) else None
        except (TimeoutError, ConnectionClosed, json.JSONDecodeError, RecursionError):
            return None
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or message.get("method") != p.M_INITIALIZE
            or "id" not in message
        ):
            return None
        try:
            return p.NodeHello.model_validate(message.get("params") or {}), message["id"]
        except ValidationError:
            return None

    async def _ping(self, session: NodeSession) -> None:
        seq = 0
        while True:
            await asyncio.sleep(self._ping_interval_s)
            seq += 1
            ends = session.cert_not_after
            if ends is not None and ends <= datetime.now(UTC):
                log.info("node.certificate_expired", device_id=session.device_id)
                self._spawn(self._close(session, CLOSE_POLICY_VIOLATION, "certificate expired"))
                return
            with contextlib.suppress(McpClientError, ConnectionClosed):
                await session.client.notify(p.M_PING, {"seq": seq, "ts": time.time()})

    # -- what a node may send ------------------------------------------------------------------

    _MODELS: dict[str, type[BaseModel] | None] = {
        p.M_PING: p.Ping,
        p.M_PONG: p.Ping,
        p.M_KILLED: p.Killed,
        p.M_CONFIRM_RESULT: p.ConfirmResult,
        p.M_SECURITY_KILL: p.SecurityKill,
        p.M_BLOB: p.Blob,
        p.M_INITIALIZED: None,
        p.M_LIST_CHANGED: None,
    }

    async def _peer(self, session: NodeSession, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        msg_id = message.get("id")
        # `initialize` is in the allow-list only as the first message (`_hello`).
        if method not in p.NODE_TO_CORE or method not in self._MODELS:
            await self._violation(session, method, msg_id, ERR_METHOD_NOT_FOUND, "not allowed")
            return
        model = self._MODELS[method]
        raw = message.get("params") or {}
        try:
            params = model.model_validate(raw) if model is not None else None
        except ValidationError:
            await self._violation(session, method, msg_id, ERR_INVALID_PARAMS, "invalid params")
            return
        session.last_seen = time.monotonic()
        if method == p.M_PING:
            assert isinstance(params, p.Ping)
            await session.client.notify(p.M_PONG, {"seq": params.seq, "ts": time.time()})
        elif method == p.M_SECURITY_KILL:
            assert isinstance(params, p.SecurityKill)
            status = self._kill(session.device_id, params.reason)
            if msg_id is not None:
                await session.answer({"jsonrpc": "2.0", "id": msg_id, "result": {"status": status}})
                return
        elif method == p.M_KILLED:
            assert isinstance(params, p.Killed)
            session.killed_calls = list(params.call_ids)
            session.killed.set()
        elif method == p.M_CONFIRM_RESULT:
            assert isinstance(params, p.ConfirmResult)
            # The decision was the device's (D1); the core records that it was asked there.
            self._append_audit(
                "node.confirm",
                session.device_id,
                "allow" if params.decision == "allow" else "deny",
                f"{p.safe_name(params.call_id, 64)}:{params.decision}",
            )
        elif method == p.M_BLOB:
            assert isinstance(params, p.Blob)
            if not self._take_chunk(session, params):
                await self._violation(session, method, msg_id, ERR_INVALID_PARAMS, "blob")
                return
        if msg_id is not None:
            await session.answer({"jsonrpc": "2.0", "id": msg_id, "result": {}})

    def _kill(self, device_id: str, reason: str) -> str:
        """A stop from the node. Never suppressed while Nox runs: every one engages the switch
        and is audited. While Nox is already stopped a repeat changes nothing; it is answered
        `already_engaged` and audited at most once per `KILL_MIN_INTERVAL_S`, so a node that
        repeats it cannot flood the audit trail."""
        now = time.monotonic()
        if self._kill_engaged is not None and self._kill_engaged():
            last = self._last_kill.get(device_id)
            if last is None or now - last >= KILL_MIN_INTERVAL_S:
                self._last_kill[device_id] = now
                self._append_audit("node.security_kill", device_id, "allow", "already_engaged")
            return "already_engaged"
        self._last_kill[device_id] = now
        self._append_audit("node.security_kill", device_id, "allow", "ok")
        if self._on_security_kill is not None:
            self._spawn(self._on_security_kill(device_id, reason))
        return "ok"

    @staticmethod
    def _expire_blobs(session: NodeSession, now: float) -> None:
        for call_id, started in list(session.blob_started.items()):
            if now - started > BLOB_TTL_S:
                session.blob_started.pop(call_id, None)
                session.complete.discard(call_id)
                dropped = session.blobs.pop(call_id, [])
                session.blob_bytes -= sum(len(c) for c in dropped)

    def _take_chunk(self, session: NodeSession, blob: p.Blob) -> bool:
        self._expire_blobs(session, time.monotonic())
        try:
            data = base64.b64decode(blob.data, validate=True)
        except ValueError:
            return False
        if blob.call_id not in session.blobs and len(session.blobs) >= MAX_PENDING_BLOBS:
            return False
        chunks = session.blobs.setdefault(blob.call_id, [])
        session.blob_started.setdefault(blob.call_id, time.monotonic())
        if blob.call_id in session.complete or blob.seq != len(chunks):
            return False
        if session.blob_bytes + len(data) > p.MAX_RESULT_BYTES:
            dropped = session.blobs.pop(blob.call_id, [])
            session.blob_bytes -= sum(len(c) for c in dropped)
            return False
        chunks.append(data)
        session.blob_bytes += len(data)
        if blob.last:
            session.complete.add(blob.call_id)
        return True

    async def _violation(
        self, session: NodeSession, method: str, msg_id: Any, code: int, why: str
    ) -> None:
        session.violations += 1
        shown = p.safe_name(method, 48)
        self._append_audit("node.refused", session.device_id, "deny", f"{why}:{shown}")
        log.warning("node.method_refused", device_id=session.device_id, method=shown)
        if msg_id is not None:
            await session.answer(
                {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": why}}
            )
        if session.violations >= MAX_VIOLATIONS:
            self._cooling[session.device_id] = time.monotonic() + VIOLATION_COOLDOWN_S
            await self._close(session, CLOSE_POLICY_VIOLATION, "too many refused messages")

    # -- revocation ----------------------------------------------------------------------------

    async def broadcast_kill(self, reason: str = "", *, wait_s: float = 0.9) -> dict[str, bool]:
        """`nox/kill` to every connected node at once (no queue: the sockets are open), then wait
        up to `wait_s` for each `nox/killed`. Device id -> whether it acknowledged in time."""
        sessions = list(self.sessions.values())
        for session in sessions:
            session.killed.clear()

        async def send(session: NodeSession) -> None:
            with contextlib.suppress(McpClientError, ConnectionClosed):
                await session.client.notify(p.M_KILL, {"reason": reason[:200]})

        await asyncio.gather(*(send(s) for s in sessions))
        waits = {s.device_id: asyncio.ensure_future(s.killed.wait()) for s in sessions}
        if waits:
            await asyncio.wait(waits.values(), timeout=wait_s)
        acked = {device_id: task.done() for device_id, task in waits.items()}
        for task in waits.values():
            task.cancel()
        for device_id, ok in acked.items():
            self._append_audit("node.kill", device_id, "allow", "ok" if ok else "no_ack")
        return acked

    async def broadcast_resume(self) -> None:
        """`nox/resume`: nodes stopped by the core go on. A stop made on a device stays."""
        for session in list(self.sessions.values()):
            with contextlib.suppress(McpClientError, ConnectionClosed):
                await session.client.notify(p.M_RESUME, {})

    async def disconnect(self, device_id: str, reason: str = "revoked") -> bool:
        """Tell the node it was removed and close its connection; True when one was open.

        The device is marked removed first - every later step of a connection that is still
        saying hello checks the mark - and the close runs under the device's lock, so it cannot
        slip between another connection's replacement and registration."""
        self._removed.add(device_id)
        async with self._lock(device_id):
            session = self.sessions.get(device_id)
            if session is None:
                return False
            with contextlib.suppress(McpClientError, ConnectionClosed, TimeoutError):
                await asyncio.wait_for(
                    session.client.notify(p.M_REVOKED, {"reason": reason[:64]}), 0.25
                )
            await self._close(session, CLOSE_POLICY_VIOLATION, "removed")
            return True

    def _lock(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock

    def _is_removed(self, device_id: str, cert: x509.Certificate | None = None) -> bool:
        """Revoked in this process, or (with `cert`) no longer a paired node in the database."""
        if device_id in self._removed:
            return True
        return cert is not None and self._pairing.identify(cert) is None

    async def _close(self, session: NodeSession, code: int, reason: str) -> None:
        if self.sessions.get(session.device_id) is session:
            del self.sessions[session.device_id]
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.connection.close(code, reason), 0.5)
        if session.connection.transport is not None:
            session.connection.transport.abort()
        await session.client.close()

    # -- helpers -------------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _append_audit(self, action: str, device_id: str, decision: str, result: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=f"node:{device_id}",
            tool="node",
            action=action,
            target=device_id,
            decision=decision,
            result=result,
        )
