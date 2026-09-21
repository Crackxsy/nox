"""`IpcHub`: the core's loopback WebSocket hub.

It implements the handshake - the first frame must be `ipc.auth` within three seconds, else the
connection is closed with a policy violation - role-scoped request dispatch through a
`RequestRegistry`, `ipc.ping` and `ipc.subscribe`, stream frames, a per-client token-bucket rate
limit, a 1 MiB frame limit, event fan-out from the injected event bus with per-role redaction, and
outbound requests to connected workers.

The hub never forwards a raw frame from one client to another: everything a client sees is either
its own answer or an event the core published.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

import nox
from nox.core.config import IpcConfig
from nox.core.events import E, Event, EventBus
from nox.core.globbing import name_matches, name_matches_any
from nox.ipc._log import get_logger
from nox.ipc.dispatch import RequestContext, RequestRegistry, service_patterns
from nox.ipc.errors import (
    CORE_SOURCE,
    ERR_AUTH_DENIED,
    ERR_INTERNAL,
    ERR_PERMISSION,
    ERR_RATE_LIMITED,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_VALIDATION,
    IpcError,
)
from nox.ipc.protocol import (
    NAME_AUTH,
    NAME_ERROR,
    NAME_PING,
    NAME_PONG,
    NAME_SUBSCRIBE,
    SCHEMA_VERSION,
    STREAM_PAYLOAD_MODELS,
    AuthRequest,
    AuthResponse,
    Envelope,
    ErrorPayload,
    Kind,
    Source,
)
from nox.ipc.tokens import TokenStore

log = get_logger(__name__)

RUNTIME_INFO_FILE = "ipc.json"
WS_PATH = "/ws"
DEFAULT_SUBSCRIPTIONS: tuple[str, ...] = ("system.*",)

CLOSE_POLICY_VIOLATION = 1008
CLOSE_GOING_AWAY = 1001

#: Outbound event filtering: (pattern, the roles that must NOT receive it).
#:
#: `plugin` is on every one of these lists. A plugin's `state.get` is carefully filtered to a
#: public subtree, and letting the same plugin subscribe to `voice.transcript_*` would have handed
#: it the raw speech that subtree exists to keep away from it. Raw chat text is blocked for the
#: same reason: the separation between the private and the stream channel is enforced here, not
#: only inside the plugin that produces it.
REDACTED_FROM: tuple[tuple[str, frozenset[str]], ...] = (
    ("voice.transcript_*", frozenset({"pet", "remote", "plugin"})),
    ("ai.response_*", frozenset({"pet", "remote", "plugin"})),
    ("memory.*", frozenset({"pet", "remote", "plugin"})),
    ("chat.*", frozenset({"pet", "remote", "plugin"})),
    ("twitch.chat_message", frozenset({"pet", "remote"})),
)
# (pattern, the only roles that may receive it)
RESTRICTED_TO: tuple[tuple[str, frozenset[str]], ...] = (
    ("security.audit", frozenset({"dashboard", "shell"})),
)


def role_may_see(role: str, name: str) -> bool:
    """Whether an event named `name` may be delivered to a client in `role`."""
    for pattern, blocked in REDACTED_FROM:
        if role in blocked and name_matches(name, pattern):
            return False
    for pattern, allowed in RESTRICTED_TO:
        if role not in allowed and name_matches(name, pattern):
            return False
    return True


_VERSION_RE = re.compile(r"^\s*v?(\d+)")


def major_version(version: str) -> int | None:
    m = _VERSION_RE.match(version)
    return int(m.group(1)) if m else None


# ---- settings -----------------------------------------------------------------------------------


class HubSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=47800, ge=0, le=65535)
    port_attempts: int = Field(default=6, ge=1, description="port and the next N-1 ports are tried")
    auth_timeout_s: float = 3.0
    max_frame_bytes: int = 1024 * 1024
    rate_per_s: float = 50.0
    rate_burst: int = 200
    send_queue_size: int = 1000
    #: Inbound events one client may have in flight. Bounded for the same reason the outbound
    #: queue is: a client inside its rate limit could otherwise grow this without limit.
    event_queue_size: int = 1000
    request_timeout_s: float = 10.0
    core_version: str = nox.__version__

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value == "localhost":
            return value
        try:
            addr = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(f"ipc.host must be a loopback address, got {value!r}") from exc
        if not addr.is_loopback:
            raise ValueError(f"ipc.host must be a loopback address, got {value!r}")
        return value

    @classmethod
    def from_config(cls, ipc: IpcConfig, **overrides: Any) -> HubSettings:
        """Build from the typed `ipc` section of `NoxConfig`."""
        return cls(host=ipc.host, port=ipc.port, **overrides)


class SubscribeRequest(BaseModel):
    patterns: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("patterns")
    @classmethod
    def _sane(cls, value: list[str]) -> list[str]:
        for p in value:
            if not p or len(p) > 96 or not re.fullmatch(r"[a-z0-9_*.]+", p):
                raise ValueError(f"invalid subscription pattern {p!r}")
        return value


class ClientInfo(BaseModel):
    client_id: str
    role: str
    session_id: str
    connected_at: datetime
    subscriptions: list[str]
    services: list[str]


# ---- helpers ------------------------------------------------------------------------------------


class TokenBucket:
    def __init__(
        self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class FrameError(Exception):
    def __init__(self, code: str, message: str, corr: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.corr = corr


def parse_frame(raw: str | bytes) -> Envelope:
    """Text JSON -> Envelope. Raises FrameError(validation.failed) for anything else."""
    if isinstance(raw, bytes):
        raise FrameError(ERR_VALIDATION, "binary frames are not accepted")
    try:
        data = json.loads(raw)
    except ValueError:
        raise FrameError(ERR_VALIDATION, "frame is not valid JSON") from None
    corr = data.get("id") if isinstance(data, dict) and isinstance(data.get("id"), str) else None
    try:
        env = Envelope.model_validate(data)
    except ValidationError as exc:
        raise FrameError(
            ERR_VALIDATION, f"invalid envelope: {exc.error_count()} error(s)", corr
        ) from None
    if env.v != SCHEMA_VERSION:
        raise FrameError(ERR_VALIDATION, f"unsupported schema version {env.v}", env.id)
    return env


def error_envelope(
    code: str, message: str, *, corr: str | None, details: dict[str, Any] | None = None
) -> Envelope:
    payload = ErrorPayload(code=code, message=message, details=details or {})
    return Envelope(
        kind=Kind.ERROR,
        name=NAME_ERROR,
        corr=corr,
        src=CORE_SOURCE,
        payload=payload.model_dump(mode="json"),
    )


def _validate_stream_payload(name: str, payload: Mapping[str, Any]) -> None:
    """Validate one STREAM frame body against `STREAM_PAYLOAD_MODELS` when `name` is registered
    An unregistered name passes through unchanged - a worker's own progress frames, say.
    """
    model = STREAM_PAYLOAD_MODELS.get(name)
    if model is None:
        return
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        raise IpcError(
            ERR_VALIDATION,
            f"invalid stream payload for {name!r}",
            details={"errors": exc.error_count()},
        ) from None


def read_runtime_info(runtime_dir: Path) -> dict[str, Any]:
    path = runtime_dir / RUNTIME_INFO_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_runtime_info(runtime_dir: Path, **fields: Any) -> Path:
    """Merge `fields` into `<runtime>/ipc.json` (ws_port from the hub, http_port from HTTP)."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    data = read_runtime_info(runtime_dir)
    data.update(fields)
    data["updated_at"] = datetime.now(UTC).isoformat()
    path = runtime_dir / RUNTIME_INFO_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---- client session -----------------------------------------------------------------------------


@dataclass
class ClientSession:
    session_id: str
    source: Source
    conn: ServerConnection
    bucket: TokenBucket
    queue: asyncio.Queue[str | None]
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    subscriptions: list[str] = field(default_factory=lambda: list(DEFAULT_SUBSCRIPTIONS))
    services: set[str] = field(default_factory=set)
    pending: dict[str, asyncio.Future[Envelope]] = field(default_factory=dict)
    stream_handlers: dict[str, Callable[[dict[str, Any]], Any]] = field(default_factory=dict)
    inflight: dict[str, str] = field(default_factory=dict)  # request id -> name (for stream frames)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Inbound events are delivered in order by `_event_pump`, off the receive loop: a slow bus
    # handler must never stop this connection's requests/responses (heartbeats, tts.finished)
    # from being read (2026-09-15 worker deadlock).
    events: asyncio.Queue[Envelope] = field(default_factory=asyncio.Queue)
    event_pump: asyncio.Task[None] | None = None
    closing: bool = False

    @property
    def client_id(self) -> str:
        return self.source.id

    @property
    def role(self) -> str:
        return self.source.role

    def info(self) -> ClientInfo:
        return ClientInfo(
            client_id=self.client_id,
            role=self.role,
            session_id=self.session_id,
            connected_at=self.connected_at,
            subscriptions=list(self.subscriptions),
            services=sorted(self.services),
        )


# ---- hub ----------------------------------------------------------------------------------------


class IpcHub:
    """The core's WebSocket hub. Construct, `await start()`, `await stop()`."""

    def __init__(
        self,
        settings: HubSettings,
        tokens: TokenStore,
        registry: RequestRegistry,
        bus: EventBus,
        runtime_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._registry = registry
        self._bus = bus
        self._runtime_dir = runtime_dir
        self._clock = clock
        self._server: Server | None = None
        self._port: int | None = None
        self._clients: dict[str, ClientSession] = {}
        self._unsubscribe: Callable[[], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Hub-owned background tasks. A task nobody holds a reference to can be garbage-collected
        #: mid-flight, and its exception is then never retrieved.
        self._tasks: set[asyncio.Task[None]] = set()

    # -- lifecycle ------------------------------------------------------------------------------

    @property
    def settings(self) -> HubSettings:
        return self._settings

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("hub not started")
        return self._port

    @property
    def url(self) -> str:
        return f"ws://{self._settings.host}:{self.port}{WS_PATH}"

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    @property
    def registry(self) -> RequestRegistry:
        return self._registry

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("hub already started")
        self._loop = asyncio.get_running_loop()
        s = self._settings
        candidates = [0] if s.port == 0 else list(range(s.port, s.port + s.port_attempts))
        last_error: OSError | None = None
        for port in candidates:
            try:
                self._server = await serve(
                    self._serve_connection,
                    s.host,
                    port,
                    process_request=self._process_request,
                    max_size=s.max_frame_bytes,
                    compression=None,
                    open_timeout=s.auth_timeout_s,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=2,
                )
            except OSError as exc:
                last_error = exc
                log.warning("ipc_port_busy", port=port, error=str(exc))
                continue
            break
        if self._server is None:
            raise OSError(f"no free IPC port in {candidates[0]}..{candidates[-1]}") from last_error
        sock = next(iter(self._server.sockets))
        self._port = int(sock.getsockname()[1])
        write_runtime_info(
            self._runtime_dir,
            host=s.host,
            ws_port=self._port,
            ws_url=self.url,
            schema_version=SCHEMA_VERSION,
            core_version=s.core_version,
            pid=os.getpid(),
        )
        self._unsubscribe = self._bus.subscribe("**", self._on_bus_event)
        log.info("ipc_hub_started", url=self.url)

    async def stop(self) -> None:
        if self._server is None:
            return
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for client in list(self._clients.values()):
            await self._close_client(client, CLOSE_GOING_AWAY, "core stopping")
        server, self._server = self._server, None
        server.close()
        await server.wait_closed()
        (self._runtime_dir / RUNTIME_INFO_FILE).unlink(missing_ok=True)
        self._port = None
        log.info("ipc_hub_stopped")

    # -- public API for the core ------------------------------------------------------------

    def clients(self) -> list[ClientInfo]:
        return [c.info() for c in self._clients.values()]

    def find_client(self, client_id: str) -> ClientInfo | None:
        c = self._find(client_id)
        return c.info() if c else None

    def declare_services(self, client_id: str, services: Iterable[str]) -> None:
        """Grant a worker/plugin its service namespaces (called by the worker.register handler)."""
        c = self._find(client_id)
        if c is None:
            raise IpcError(ERR_UNAVAILABLE, f"client {client_id!r} is not connected")
        c.services.update(services)

    async def stream(
        self,
        client_id: str,
        corr: str,
        payload: dict[str, Any],
        done: bool = False,
        *,
        name: str | None = None,
    ) -> None:
        """Send a stream frame for request `corr` to `client_id`; the last one carries done=true."""
        c = self._find(client_id)
        if c is None:
            raise IpcError(ERR_UNAVAILABLE, f"client {client_id!r} is not connected")
        frame_name = name or c.inflight.get(corr) or "ipc.stream"
        _validate_stream_payload(frame_name, payload)
        body = dict(payload)
        body["done"] = done
        self._enqueue(
            c, Envelope(kind=Kind.STREAM, name=frame_name, corr=corr, src=CORE_SOURCE, payload=body)
        )

    async def request(
        self,
        client_id: str,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_stream: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Send a request to a connected worker/plugin and return its response payload."""
        c = self._find(client_id)
        if c is None:
            raise IpcError(
                ERR_UNAVAILABLE, f"client {client_id!r} is not connected", retryable=True
            )
        env = Envelope(kind=Kind.REQUEST, name=name, src=CORE_SOURCE, payload=dict(payload or {}))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Envelope] = loop.create_future()
        c.pending[env.id] = fut
        if on_stream is not None:
            c.stream_handlers[env.id] = on_stream
        try:
            self._enqueue(c, env)
            reply = await asyncio.wait_for(fut, timeout or self._settings.request_timeout_s)
        except TimeoutError:
            raise IpcError(
                ERR_TIMEOUT, f"{name} to {client_id} timed out", retryable=True
            ) from None
        finally:
            c.pending.pop(env.id, None)
            c.stream_handlers.pop(env.id, None)
        if reply.kind is Kind.ERROR:
            raise IpcError.from_payload(reply.payload)
        return reply.payload

    async def disconnect(self, client_id: str, reason: str = "disconnected by core") -> None:
        c = self._find(client_id)
        if c is not None:
            await self._close_client(c, CLOSE_POLICY_VIOLATION, reason)

    # -- connection handling ---------------------------------------------------------------------

    def _find(self, client_id: str) -> ClientSession | None:
        found = [c for c in self._clients.values() if c.client_id == client_id and not c.closing]
        if not found:
            return None
        return max(found, key=lambda c: c.connected_at)

    def _process_request(self, conn: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path != WS_PATH:
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")
        return None

    async def _serve_connection(self, conn: ServerConnection) -> None:
        client = await self._handshake(conn)
        if client is None:
            return
        self._clients[client.session_id] = client
        writer = asyncio.create_task(self._writer(client))
        client.event_pump = asyncio.create_task(self._event_pump(client))
        log.info("ipc_client_connected", client=client.client_id, role=client.role)
        await self._publish(
            E.IPC_CLIENT_CONNECTED, {"client_id": client.client_id, "role": client.role}
        )
        try:
            while True:
                try:
                    raw = await conn.recv()
                except ConnectionClosed:
                    break
                if not await self._on_frame(client, raw):
                    break
        finally:
            client.closing = True
            self._clients.pop(client.session_id, None)
            for task in list(client.tasks):
                task.cancel()
            for fut in client.pending.values():
                if not fut.done():
                    fut.set_exception(
                        IpcError(ERR_UNAVAILABLE, "client disconnected", retryable=True)
                    )
            client.pending.clear()
            writer.cancel()
            pump = client.event_pump
            if pump is not None:
                pump.cancel()
            await asyncio.gather(
                writer, *([pump] if pump is not None else []), return_exceptions=True
            )
            log.info(
                "ipc_client_disconnected",
                client=client.client_id,
                role=client.role,
                code=conn.close_code,
            )
            await self._publish(
                E.IPC_CLIENT_DISCONNECTED,
                {"client_id": client.client_id, "role": client.role, "code": conn.close_code},
            )

    async def _handshake(self, conn: ServerConnection) -> ClientSession | None:
        s = self._settings
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=s.auth_timeout_s)
        except TimeoutError:
            log.warning("ipc_auth_timeout", peer=_peer(conn))
            await conn.close(CLOSE_POLICY_VIOLATION, "auth timeout")
            return None
        except ConnectionClosed:
            return None
        try:
            env = parse_frame(raw)
        except FrameError as exc:
            await self._deny(conn, exc.corr, f"first frame must be {NAME_AUTH}: {exc.message}")
            return None
        if env.kind is not Kind.REQUEST or env.name != NAME_AUTH:
            await self._deny(conn, env.id, f"first frame must be request {NAME_AUTH}")
            return None
        try:
            auth = AuthRequest.model_validate(env.payload)
        except ValidationError:
            await self._deny(conn, env.id, "malformed auth payload")
            return None
        if auth.role != env.src.role or auth.id != env.src.id:
            await self._deny(conn, env.id, "auth payload and envelope source disagree")
            return None
        if auth.client_version:
            client_major = major_version(auth.client_version)
            if client_major is None or client_major != major_version(s.core_version):
                await self._deny(
                    conn, env.id, f"client version {auth.client_version} not compatible"
                )
                return None
        decision = self._tokens.authenticate(auth.token, auth.role, auth.id)
        if not decision.ok:
            log.warning("ipc_auth_denied", role=auth.role, client=auth.id, reason=decision.reason)
            await self._deny(conn, env.id, decision.reason)
            return None
        session_id = str(uuid.uuid4())
        client = ClientSession(
            session_id=session_id,
            source=Source(role=auth.role, id=auth.id),
            conn=conn,
            bucket=TokenBucket(s.rate_per_s, s.rate_burst, self._clock),
            queue=asyncio.Queue(maxsize=s.send_queue_size),
            events=asyncio.Queue(maxsize=s.event_queue_size),
        )
        response = AuthResponse(ok=True, session_id=session_id, core_version=s.core_version)
        await conn.send(
            env.reply(NAME_AUTH, response.model_dump(mode="json"), CORE_SOURCE).model_dump_json()
        )
        return client

    async def _deny(self, conn: ServerConnection, corr: str | None, reason: str) -> None:
        try:
            await conn.send(error_envelope(ERR_AUTH_DENIED, reason, corr=corr).model_dump_json())
            await conn.close(CLOSE_POLICY_VIOLATION, "auth denied")
        except ConnectionClosed:
            pass
        return None

    async def _on_frame(self, client: ClientSession, raw: str | bytes) -> bool:
        """Handle one frame after auth. Returns False when the connection must end."""
        if not client.bucket.allow():
            log.warning("ipc_rate_limited", client=client.client_id, role=client.role)
            await self._send_now(
                client, error_envelope(ERR_RATE_LIMITED, "rate limit exceeded", corr=None)
            )
            await self._close_client(client, CLOSE_POLICY_VIOLATION, "rate limited")
            return False
        try:
            env = parse_frame(raw)
        except FrameError as exc:
            log.debug("ipc_bad_frame", client=client.client_id, reason=exc.message)
            self._enqueue(client, error_envelope(exc.code, exc.message, corr=exc.corr))
            return True
        if env.src != client.source:
            self._enqueue(
                client,
                error_envelope(
                    ERR_PERMISSION, "envelope source does not match the session", corr=env.id
                ),
            )
            return True
        if env.kind is Kind.REQUEST:
            task = asyncio.create_task(self._handle_request(client, env))
            client.tasks.add(task)
            task.add_done_callback(client.tasks.discard)
        elif env.kind in (Kind.RESPONSE, Kind.ERROR):
            fut = client.pending.get(env.corr or "")
            if fut is not None and not fut.done():
                fut.set_result(env)
        elif env.kind is Kind.STREAM:
            handler = client.stream_handlers.get(env.corr or "")
            if handler is not None:
                await _maybe_await(handler(env.payload))
        elif env.kind is Kind.EVENT:
            try:
                client.events.put_nowait(env)
            except asyncio.QueueFull:
                # The same rule the outbound queue follows: a client that produces faster than the
                # core can consume is disconnected, not buffered without limit.
                log.warning("ipc_event_backlog_full", client=client.client_id, role=client.role)
                await self._close_client(client, CLOSE_POLICY_VIOLATION, "event backlog")
                return False
        return True

    async def _event_pump(self, client: ClientSession) -> None:
        while True:
            env = await client.events.get()
            try:
                await self._handle_inbound_event(client, env)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad event never ends the pump
                log.error(
                    "ipc_event_failed", client=client.client_id, name=env.name, error=str(exc)
                )

    async def _handle_request(self, client: ClientSession, env: Envelope) -> None:
        """Answer exactly one request with exactly one frame, whatever happens inside it."""
        client.inflight[env.id] = env.name
        try:
            if env.name == NAME_AUTH:
                reply = error_envelope(ERR_VALIDATION, "already authenticated", corr=env.id)
            elif env.name == NAME_PING:
                reply = env.reply(
                    NAME_PONG,
                    {**env.payload, "server_ts": datetime.now(UTC).isoformat()},
                    CORE_SOURCE,
                )
            elif env.name == NAME_SUBSCRIBE:
                reply = self._subscribe(client, env)
            else:
                ctx = RequestContext(
                    client_id=client.client_id,
                    role=client.role,
                    request=env,
                    services=frozenset(client.services),
                    stream=self._stream_fn(client, env),
                )
                reply = await self._registry.dispatch(ctx, env)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a caller must never wait out its own timeout
            log.exception("ipc_request_crashed", client=client.client_id, name=env.name)
            reply = error_envelope(
                ERR_INTERNAL,
                f"{env.name} failed inside the hub",
                corr=env.id,
                details={"type": type(exc).__name__},
            )
        finally:
            client.inflight.pop(env.id, None)
        self._enqueue(client, reply)

    def _stream_fn(
        self, client: ClientSession, env: Envelope
    ) -> Callable[[dict[str, Any], bool], Any]:
        async def _stream(payload: dict[str, Any], done: bool = False) -> None:
            _validate_stream_payload(env.name, payload)
            body = dict(payload)
            body["done"] = done
            self._enqueue(
                client,
                Envelope(
                    kind=Kind.STREAM, name=env.name, corr=env.id, src=CORE_SOURCE, payload=body
                ),
            )

        return _stream

    def _subscribe(self, client: ClientSession, env: Envelope) -> Envelope:
        try:
            req = SubscribeRequest.model_validate(env.payload)
        except ValidationError as exc:
            return error_envelope(
                ERR_VALIDATION,
                "invalid subscribe payload",
                corr=env.id,
                details={"errors": exc.error_count()},
            )
        client.subscriptions = list(dict.fromkeys([*DEFAULT_SUBSCRIPTIONS, *req.patterns]))
        return env.reply(NAME_SUBSCRIBE, {"patterns": list(client.subscriptions)}, CORE_SOURCE)

    async def _handle_inbound_event(self, client: ClientSession, env: Envelope) -> None:
        allowed = client.role in ("worker", "plugin") and name_matches_any(
            env.name, service_patterns(client.services)
        )
        if not allowed:
            self._enqueue(
                client,
                error_envelope(
                    ERR_PERMISSION,
                    f"role {client.role!r} may not publish {env.name!r}",
                    corr=env.id,
                ),
            )
            return
        try:
            await self._bus.publish(
                Event(name=env.name, payload=env.payload, corr=env.corr, source=client.client_id)
            )
        except (ValidationError, ValueError) as exc:
            self._enqueue(
                client,
                error_envelope(
                    ERR_VALIDATION, f"event payload rejected: {type(exc).__name__}", corr=env.id
                ),
            )

    # -- event fan-out --------------------------------------------------------------------------

    def _on_bus_event(self, event: Event) -> None:
        if not self._clients:
            return
        serialized: str | None = None
        for client in list(self._clients.values()):
            if client.closing or not role_may_see(client.role, event.name):
                continue
            if not name_matches_any(event.name, client.subscriptions):
                continue
            if serialized is None:
                serialized = Envelope(
                    kind=Kind.EVENT,
                    name=event.name,
                    corr=event.corr,
                    src=CORE_SOURCE,
                    payload=event.payload,
                ).model_dump_json()
            self._enqueue_text(client, serialized)

    async def _publish(self, name: str, payload: dict[str, Any]) -> None:
        try:
            await self._bus.publish(Event(name=name, payload=payload, source="ipc"))
        except Exception:  # noqa: BLE001 - a connection notice must not break the connection
            log.exception("ipc_event_publish_failed", name=name)

    # -- sending ------------------------------------------------------------

    def _enqueue(self, client: ClientSession, env: Envelope) -> None:
        self._enqueue_text(client, env.model_dump_json())

    def _enqueue_text(self, client: ClientSession, text: str) -> None:
        if client.closing:
            return
        try:
            client.queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("ipc_slow_consumer", client=client.client_id, role=client.role)
            client.closing = True
            if self._loop is not None:
                self._spawn(self._close_client(client, CLOSE_POLICY_VIOLATION, "slow consumer"))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run `coro` in the background, holding a reference until it finishes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_now(self, client: ClientSession, env: Envelope) -> None:
        try:
            await client.conn.send(env.model_dump_json())
        except ConnectionClosed:
            pass

    async def _writer(self, client: ClientSession) -> None:
        while True:
            item = await client.queue.get()
            if item is None:
                return
            try:
                await client.conn.send(item)
            except ConnectionClosed:
                return

    async def _close_client(self, client: ClientSession, code: int, reason: str) -> None:
        client.closing = True
        try:
            await client.conn.close(code, reason)
        except ConnectionClosed:
            pass


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        await value


def _peer(conn: ServerConnection) -> str:
    try:
        return str(conn.remote_address)
    except OSError as exc:  # a closed socket has no peer; this string only reaches a log line
        return f"?({type(exc).__name__})"
