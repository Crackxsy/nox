"""obs-websocket v5 client (ST-11-02/03, Spec v0.2 Stream Bot §6.1).

Implements exactly the handshake and framing obs-websocket v5 requires: Hello (op 0) -> Identify
(op 1, SHA256 challenge/salt authentication when OBS requires a password) -> Identified (op 2),
then Request/RequestResponse (op 6/7) correlated by `requestId`, and Event (op 5) dispatch. Uses
only the `websockets` library (already a project dependency) - no OBS SDK, no extra deps.

The connection owns its own reconnect-with-backoff loop (`start()`/`stop()`); callers observe
`connected`/`identified` and get `on_connected`/`on_disconnected` callbacks to drive honest health
reporting one layer up (`nox_plugin_obs.plugin`).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import websockets

from nox.core.logging import get_logger

log = get_logger(__name__)

# ---- obs-websocket v5 wire protocol (protocol.md) ------------------------------------------------

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

RPC_VERSION = 1

# EventSubscription bitmask (only the categories this plugin needs events from).
EVENT_SUB_GENERAL = 1 << 0  # ExitStarted, VendorEvent, ...
EVENT_SUB_SCENES = 1 << 2  # CurrentProgramSceneChanged, SceneListChanged, ...
EVENT_SUB_OUTPUTS = 1 << 6  # StreamStateChanged, RecordStateChanged, ...
DEFAULT_EVENT_SUBSCRIPTIONS = EVENT_SUB_GENERAL | EVENT_SUB_SCENES | EVENT_SUB_OUTPUTS


class ObsProtocolError(RuntimeError):
    """The peer did not follow the Hello -> Identify -> Identified handshake."""


class ObsAuthRequiredError(RuntimeError):
    """OBS's Hello carried an authentication challenge but no password is available."""


class ObsRequestError(RuntimeError):
    """A Request came back with `requestStatus.result: false`."""

    def __init__(self, request_type: str, code: int, comment: str) -> None:
        super().__init__(f"{request_type} failed (code {code}): {comment}")
        self.request_type = request_type
        self.code = code
        self.comment = comment


def compute_auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 auth: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ConnectedHook = Callable[[], Awaitable[None]]
DisconnectedHook = Callable[[str], Awaitable[None]]
PasswordProvider = Callable[[], Awaitable[str | None]]


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> Any: ...


class ObsWebSocketClient:
    """One obs-websocket v5 connection, reconnecting with exponential backoff until `stop()`."""

    def __init__(
        self,
        url: str,
        *,
        password_provider: PasswordProvider,
        on_event: EventHandler,
        on_connected: ConnectedHook | None = None,
        on_disconnected: DisconnectedHook | None = None,
        event_subscriptions: int = DEFAULT_EVENT_SUBSCRIPTIONS,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        request_timeout_s: float = 5.0,
        connector: Callable[[str], Any] | None = None,
        authorize: Callable[[], None] | None = None,
    ) -> None:
        self.url = url
        self._password_provider = password_provider
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self.event_subscriptions = event_subscriptions
        #: Called synchronously before every connection attempt (ADR-013: raw websockets bypass
        #: `PluginApi.http()`'s automatic `EgressGuard`, so this plugin enforces it itself). Raises
        #: `EgressDenied` (a `PermissionError`) when the endpoint or the current privacy mode
        #: disallows it; that failure flows through the normal reconnect/backoff path like any
        #: other connection error.
        self._authorize = authorize
        self._min_backoff = min_backoff_s
        self._max_backoff = max_backoff_s
        self._request_timeout = request_timeout_s
        self._connector = connector or websockets.connect
        self._ws: WebSocketLike | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.identified = False
        self.last_error = ""
        self.backoff_s = min_backoff_s

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="obs-ws-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001 - best-effort close during shutdown
                log.debug("obs.ws_close_failed", error=str(exc))
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.connected = False
        self.identified = False

    async def _run(self) -> None:
        backoff = self._min_backoff
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = self._min_backoff
            except Exception as exc:  # noqa: BLE001 - the reconnect loop must never die
                self.last_error = str(exc)
            was_connected = self.connected
            self.connected = False
            self.identified = False
            self._fail_pending(self.last_error or "disconnected")
            if was_connected and self._on_disconnected is not None:
                await self._on_disconnected(self.last_error)
            if self._stop.is_set():
                return
            self.backoff_s = backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, self._max_backoff)

    async def _connect_once(self) -> None:
        if self._authorize is not None:
            self._authorize()
        async with self._connector(self.url) as ws:
            self._ws = ws
            hello = json.loads(await ws.recv())
            if hello.get("op") != OP_HELLO:
                raise ObsProtocolError(f"expected Hello, got op={hello.get('op')!r}")
            data = hello.get("d", {})
            identify: dict[str, Any] = {
                "rpcVersion": RPC_VERSION,
                "eventSubscriptions": self.event_subscriptions,
            }
            auth = data.get("authentication")
            if auth:
                password = await self._password_provider()
                if not password:
                    raise ObsAuthRequiredError(
                        "OBS requires a password but nox/obs/websocket_password is not set"
                    )
                identify["authentication"] = compute_auth_string(
                    password, str(auth["salt"]), str(auth["challenge"])
                )
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
            identified = json.loads(await ws.recv())
            if identified.get("op") != OP_IDENTIFIED:
                raise ObsProtocolError(f"Identify was refused: {identified}")
            self.connected = True
            self.identified = True
            self.last_error = ""
            if self._on_connected is not None:
                await self._on_connected()
            async for raw in ws:
                await self._dispatch(json.loads(raw))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        d = message.get("d", {}) or {}
        if op == OP_REQUEST_RESPONSE:
            request_id = str(d.get("requestId", ""))
            future = self._pending.pop(request_id, None)
            if future is None or future.done():
                return
            status = d.get("requestStatus", {}) or {}
            if status.get("result"):
                future.set_result(d.get("responseData", {}) or {})
            else:
                future.set_exception(
                    ObsRequestError(
                        str(d.get("requestType", "")),
                        int(status.get("code", 0)),
                        str(status.get("comment", "")),
                    )
                )
        elif op == OP_EVENT:
            result = self._on_event(str(d.get("eventType", "")), d.get("eventData", {}) or {})
            if result is not None:
                await result

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self._pending.clear()

    # -- requests ------------------------------------------------------------------------------

    async def request(
        self,
        request_type: str,
        request_data: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._ws is None or not self.identified:
            raise ConnectionError(f"not connected to OBS ({self.last_error or 'no session'})")
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        payload: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if request_data:
            payload["requestData"] = dict(request_data)
        await self._ws.send(json.dumps({"op": OP_REQUEST, "d": payload}))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self._request_timeout)
        finally:
            self._pending.pop(request_id, None)
