"""IpcClient: asyncio WebSocket client for shell, workers, plugins and tests (IPC Model, ADR-003).

Handshake (`ipc.auth` as first frame), request/response by `corr`, stream consumption as an async
iterator, event subscriptions with glob patterns, inbound request handlers (workers answer
`stt.*`/`tts.*` requests from the core) and optional auto-reconnect with exponential backoff.
`IpcClientThread` runs a client on its own loop in a background thread for asyncio-less hosts (Qt).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from nox.ipc._log import get_logger
from nox.ipc.dispatch import match_name
from nox.ipc.errors import (
    ERR_AUTH_DENIED,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    IpcError,
)
from nox.ipc.protocol import (
    NAME_AUTH,
    NAME_ERROR,
    NAME_SUBSCRIBE,
    AuthRequest,
    AuthResponse,
    Envelope,
    Kind,
    Source,
)

log = get_logger(__name__)

EventHandler = Callable[[Envelope], Awaitable[None] | None]
InboundHandler = Callable[
    [dict[str, Any], "InboundRequest"], Awaitable[BaseModel | Mapping[str, Any] | None]
]
_END = object()


def _mark_retrieved(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()


class StreamCall:
    """Async iterator over the stream frames of one request; `await result()` is the response."""

    def __init__(self, request_id: str, name: str, timeout: float, *, collect: bool) -> None:
        self.request_id = request_id
        self.name = name
        self._timeout = timeout
        self._collect = collect
        self._chunks: asyncio.Queue[Any] = asyncio.Queue()
        self._result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        # A caller that already gave up (timeout) never looks at a later `_fail`; mark the
        # exception retrieved so asyncio does not log "Future exception was never retrieved".
        self._result.add_done_callback(_mark_retrieved)

    def _on_stream(self, payload: dict[str, Any]) -> None:
        if self._collect:
            self._chunks.put_nowait(payload)
            if payload.get("done") is True:
                self._chunks.put_nowait(_END)

    def _finish(self, env: Envelope) -> None:
        if not self._result.done():
            if env.kind is Kind.ERROR:
                self._result.set_exception(IpcError.from_payload(env.payload))
            else:
                self._result.set_result(env.payload)
        self._chunks.put_nowait(_END)

    def _fail(self, exc: IpcError) -> None:
        if not self._result.done():
            self._result.set_exception(exc)
        self._chunks.put_nowait(_END)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                item = await asyncio.wait_for(self._chunks.get(), self._timeout)
            except TimeoutError:
                self._fail(IpcError(ERR_TIMEOUT, f"{self.name} stream timed out", retryable=True))
                return
            if item is _END:
                return
            yield item

    async def result(self) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.shield(self._result), self._timeout)
        except TimeoutError:
            self._fail(IpcError(ERR_TIMEOUT, f"{self.name} timed out", retryable=True))
            raise IpcError(ERR_TIMEOUT, f"{self.name} timed out", retryable=True) from None


@dataclass
class InboundRequest:
    """Context for a request the core sent to this client."""

    envelope: Envelope
    _client: IpcClient = field(repr=False)

    async def stream(self, payload: Mapping[str, Any], done: bool = False) -> None:
        body = dict(payload)
        body["done"] = done
        await self._client._send(
            Envelope(
                kind=Kind.STREAM,
                name=self.envelope.name,
                corr=self.envelope.id,
                src=self._client.source,
                payload=body,
            )
        )


class IpcClient:
    def __init__(
        self,
        url: str,
        token: str,
        role: str,
        id: str,  # noqa: A002 - mirrors AuthRequest.id
        *,
        client_version: str = "",
        reconnect: bool = False,
        backoff_initial_s: float = 0.2,
        backoff_max_s: float = 5.0,
        request_timeout_s: float = 10.0,
        on_connection_change: Callable[[bool], Any] | None = None,
    ) -> None:
        self.url = url
        self._token = token
        self.source = Source(role=role, id=id)
        self._client_version = client_version
        self._reconnect = reconnect
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._timeout = request_timeout_s
        self._on_connection_change = on_connection_change
        self._conn: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._reconnector: asyncio.Task[None] | None = None
        self._closing = False
        self._pending: dict[str, StreamCall] = {}
        self._subscriptions: list[str] = []
        self._event_handlers: list[tuple[str, EventHandler]] = []
        self._inbound: dict[str, InboundHandler] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.session_id: str | None = None
        self.auth: AuthResponse | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # -- lifecycle ------------------------------------------------------------------------------

    async def connect(self) -> AuthResponse:
        self._closing = False
        return await self._open()

    async def _open(self) -> AuthResponse:
        try:
            conn = await connect(self.url, max_size=1024 * 1024, compression=None, open_timeout=5)
        except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as exc:
            raise IpcError(
                ERR_UNAVAILABLE, f"cannot connect to {self.url}: {exc}", retryable=True
            ) from exc
        auth = AuthRequest(
            token=self._token,
            role=self.source.role,
            id=self.source.id,
            client_version=self._client_version,
        )
        req = Envelope(
            kind=Kind.REQUEST, name=NAME_AUTH, src=self.source, payload=auth.model_dump(mode="json")
        )
        try:
            await conn.send(req.model_dump_json())
            raw = await asyncio.wait_for(conn.recv(), 5)
            env = Envelope.model_validate_json(raw)
        except ConnectionClosed as exc:
            raise IpcError(ERR_AUTH_DENIED, f"connection closed during auth ({exc.rcvd})") from exc
        except (TimeoutError, ValidationError) as exc:
            await conn.close()
            raise IpcError(
                ERR_UNAVAILABLE, f"auth handshake failed: {exc}", retryable=True
            ) from exc
        if env.kind is Kind.ERROR:
            await conn.close()
            raise IpcError.from_payload(env.payload)
        response = AuthResponse.model_validate(env.payload)
        if not response.ok:
            await conn.close()
            raise IpcError(ERR_AUTH_DENIED, response.reason or "auth rejected")
        self._conn = conn
        self.auth = response
        self.session_id = response.session_id
        self._reader = asyncio.create_task(self._read_loop(conn))
        if self._subscriptions:
            await self._send_subscribe()
        self._notify(True)
        return response

    async def close(self) -> None:
        self._closing = True
        if self._reconnector is not None:
            self._reconnector.cancel()
            await asyncio.gather(self._reconnector, return_exceptions=True)
            self._reconnector = None
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        for task in list(self._tasks):
            task.cancel()
        self._fail_pending(IpcError(ERR_UNAVAILABLE, "client closed"))

    # -- requests -------------------------------------------------------------------------------

    async def request(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        call = await self._start_call(name, payload, timeout, collect=False)
        return await call.result()

    async def request_stream(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> StreamCall:
        """Send a request answered by stream frames: iterate the result, then `await result()`."""
        return await self._start_call(name, payload, timeout, collect=True)

    async def _start_call(
        self, name: str, payload: Mapping[str, Any] | None, timeout: float | None, *, collect: bool
    ) -> StreamCall:
        if self._conn is None:
            raise IpcError(ERR_UNAVAILABLE, "not connected", retryable=True)
        env = Envelope(kind=Kind.REQUEST, name=name, src=self.source, payload=dict(payload or {}))
        call = StreamCall(env.id, name, timeout or self._timeout, collect=collect)
        self._pending[env.id] = call
        try:
            await self._send(env)
        except IpcError:
            self._pending.pop(env.id, None)
            raise
        return call

    async def send_event(
        self, name: str, payload: Mapping[str, Any] | None = None, *, corr: str | None = None
    ) -> None:
        """Publish an event (workers/plugins only, within their declared namespace)."""
        await self._send(
            Envelope(
                kind=Kind.EVENT, name=name, corr=corr, src=self.source, payload=dict(payload or {})
            )
        )

    # -- events ---------------------------------------------------------------------------------

    async def subscribe(
        self, patterns: Iterable[str], handler: EventHandler | None = None
    ) -> list[str]:
        """Add glob patterns to the server-side subscription, optionally with a handler for them."""
        new = [p for p in patterns if p not in self._subscriptions]
        self._subscriptions.extend(new)
        if handler is not None:
            for p in patterns:
                self._event_handlers.append((p, handler))
        if self._conn is not None:
            await self._send_subscribe()
        return list(self._subscriptions)

    def on(self, name_glob: str, handler: EventHandler) -> Callable[[], None]:
        """Attach a local handler for already-subscribed events. Returns a remover."""
        entry = (name_glob, handler)
        self._event_handlers.append(entry)

        def remove() -> None:
            if entry in self._event_handlers:
                self._event_handlers.remove(entry)

        return remove

    def handle(self, name: str, handler: InboundHandler) -> None:
        """Register a handler for requests the core sends to this client (e.g. `stt.transcribe`)."""
        self._inbound[name] = handler

    async def _send_subscribe(self) -> None:
        env = Envelope(
            kind=Kind.REQUEST,
            name=NAME_SUBSCRIBE,
            src=self.source,
            payload={"patterns": list(self._subscriptions)},
        )
        call = StreamCall(env.id, NAME_SUBSCRIBE, self._timeout, collect=False)
        self._pending[env.id] = call
        await self._send(env)
        await call.result()

    # -- internals --------------------------------------------------------------------------------

    async def _send(self, env: Envelope) -> None:
        if self._conn is None:
            raise IpcError(ERR_UNAVAILABLE, "not connected", retryable=True)
        try:
            await self._conn.send(env.model_dump_json())
        except ConnectionClosed as exc:
            raise IpcError(
                ERR_UNAVAILABLE, f"connection closed ({exc.rcvd})", retryable=True
            ) from exc

    async def _read_loop(self, conn: ClientConnection) -> None:
        try:
            while True:
                try:
                    raw = await conn.recv()
                except ConnectionClosed:
                    break
                try:
                    env = Envelope.model_validate_json(raw)
                except ValidationError:
                    log.debug("ipc_client_bad_frame", client=self.source.id)
                    continue
                self._dispatch(env)
        finally:
            if self._conn is conn:
                self._conn = None
                self._fail_pending(IpcError(ERR_UNAVAILABLE, "connection lost", retryable=True))
                self._notify(False)
                if self._reconnect and not self._closing:
                    self._reconnector = asyncio.create_task(self._reconnect_loop())

    def _dispatch(self, env: Envelope) -> None:
        if env.kind in (Kind.RESPONSE, Kind.ERROR):
            call = self._pending.pop(env.corr or "", None)
            if call is not None:
                call._finish(env)
            elif env.kind is Kind.ERROR and env.name == NAME_ERROR:
                log.warning(
                    "ipc_unsolicited_error", client=self.source.id, code=env.payload.get("code")
                )
        elif env.kind is Kind.STREAM:
            call = self._pending.get(env.corr or "")
            if call is not None:
                call._on_stream(env.payload)
        elif env.kind is Kind.EVENT:
            for pattern, handler in list(self._event_handlers):
                if match_name(pattern, env.name):
                    self._spawn(_call_handler(handler, env))
        elif env.kind is Kind.REQUEST:
            self._spawn(self._answer(env))

    async def _answer(self, env: Envelope) -> None:
        handler = self._inbound.get(env.name)
        try:
            if handler is None:
                raise IpcError(ERR_NOT_FOUND, f"{self.source.id} does not handle {env.name!r}")
            result = await handler(env.payload, InboundRequest(env, self))
            payload = (
                result.model_dump(mode="json")
                if isinstance(result, BaseModel)
                else dict(result or {})
            )
            reply = env.reply(env.name, payload, self.source)
        except IpcError as exc:
            reply = env.reply(NAME_ERROR, exc.to_payload(), self.source, kind=Kind.ERROR)
        except Exception as exc:
            # `log.exception(...)` can itself raise `UnicodeEncodeError` on a Windows console
            # (cp1252/cp437 stdout/stderr) when the original exception message contains
            # non-encodable characters - that would skip the reply below and hang the caller.
            # Log defensively (string-only fields, never re-raises) and always send the error
            # reply regardless of what logging did.
            try:
                log.error(
                    "ipc_inbound_handler_failed",
                    name=env.name,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
            except Exception:  # noqa: S110 - logging must never be able to skip the reply below
                pass
            err = IpcError(
                ERR_INTERNAL,
                f"handler for {env.name!r} failed",
                details={"type": type(exc).__name__},
            )
            reply = env.reply(NAME_ERROR, err.to_payload(), self.source, kind=Kind.ERROR)
        try:
            await self._send(reply)
        except IpcError:
            pass

    async def _reconnect_loop(self) -> None:
        delay = self._backoff_initial
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self._open()
            except IpcError as exc:
                if exc.code == ERR_AUTH_DENIED:
                    log.error("ipc_reconnect_denied", client=self.source.id, reason=exc.message)
                    return
                log.debug("ipc_reconnect_failed", client=self.source.id, delay=delay)
                delay = min(delay * 2, self._backoff_max)
                continue
            log.info("ipc_reconnected", client=self.source.id)
            return

    def _fail_pending(self, exc: IpcError) -> None:
        pending, self._pending = self._pending, {}
        for call in pending.values():
            call._fail(exc)

    def _notify(self, connected: bool) -> None:
        if self._on_connection_change is not None:
            try:
                self._on_connection_change(connected)
            except Exception:
                log.exception("ipc_connection_callback_failed")

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _call_handler(handler: EventHandler, env: Envelope) -> None:
    try:
        result = handler(env)
        if result is not None:
            await result
    except Exception:
        log.exception("ipc_event_handler_failed", name=env.name)


class IpcClientThread:
    """Runs an IpcClient on a private asyncio loop in a daemon thread.

    `call()` returns a concurrent Future; event callbacks (`events` gets the Envelope, `on_event`
    gets its dict form) run on the client thread - marshal to the UI thread yourself (Qt: emit a
    Signal). Subscribes to `**` by default (the hub still filters per role); reconnect is on.
    """

    def __init__(
        self,
        url: str,
        token: str,
        role: str,
        id: str,  # noqa: A002
        *,
        events: Callable[[Envelope], None] | None = None,
        patterns: Iterable[str] = ("**",),
        reconnect: bool = True,
        on_connection_change: Callable[[bool], Any] | None = None,
    ) -> None:
        self._args = (url, token, role, id)
        self._events = events
        self._dict_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._patterns = list(patterns)
        self._reconnect = reconnect
        self._on_connection_change = on_connection_change
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: IpcClient | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback receiving each event as a dict (shell IpcBridge protocol)."""
        self._dict_callbacks.append(callback)

    def _fan_out(self, env: Envelope) -> None:
        if self._events is not None:
            self._events(env)
        if self._dict_callbacks:
            data = env.model_dump(mode="json")
            for cb in list(self._dict_callbacks):
                cb(data)

    def start(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(target=self._run, name="nox-ipc-client", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise IpcError(ERR_TIMEOUT, "client thread did not start in time")
        if self._startup_error is not None:
            raise self._startup_error

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._startup())
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            self._startup_error = exc
            loop.close()
            self._loop = None  # nothing left to stop; stop() must not touch a closed loop
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _startup(self) -> None:
        url, token, role, id_ = self._args
        client = IpcClient(
            url,
            token,
            role,
            id_,
            reconnect=self._reconnect,
            on_connection_change=self._on_connection_change,
        )
        client.on("**", self._fan_out)
        if self._patterns:
            await client.subscribe(self._patterns)
        await client.connect()
        self._client = client

    def call(
        self, name: str, payload: Mapping[str, Any] | None = None, *, timeout: float | None = None
    ) -> concurrent.futures.Future[dict[str, Any]]:
        if self._loop is None or self._client is None:
            raise IpcError(ERR_UNAVAILABLE, "client thread not running")
        return asyncio.run_coroutine_threadsafe(
            self._client.request(name, payload, timeout=timeout), self._loop
        )

    def subscribe(self, patterns: Iterable[str]) -> concurrent.futures.Future[list[str]]:
        if self._loop is None or self._client is None:
            raise IpcError(ERR_UNAVAILABLE, "client thread not running")
        return asyncio.run_coroutine_threadsafe(self._client.subscribe(list(patterns)), self._loop)

    def stop(self, timeout: float = 5.0) -> None:
        loop, client, thread = self._loop, self._client, self._thread
        if loop is None or thread is None:
            return
        if client is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout)
            except (concurrent.futures.TimeoutError, RuntimeError):
                pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:  # loop already closed by a failed startup
            pass
        thread.join(timeout)
        self._loop = None
        self._client = None
        self._thread = None
