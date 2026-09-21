"""Home Assistant WebSocket API client.

Speaks exactly the documented handshake and framing of `ws://<host>:8123/api/websocket`:
`auth_required` -> `auth` (long-lived access token) -> `auth_ok` | `auth_invalid`, then commands
with a monotonically increasing `id` answered by `{"type": "result", "success": ...}`, and
`{"type": "event"}` frames for every subscription. Uses only the `websockets` library, which Nox
already depends on - there is no Home Assistant SDK in this tree and no third-party client to
audit.

The connection owns its own reconnect-with-backoff loop (`start`/`stop`); callers observe
`connected`/`authenticated` and get `on_connected`/`on_disconnected` callbacks so health one layer
up can be reported honestly instead of guessed.

Modelled on `nox_plugin_obs.ws_client`; the two are deliberately similar so a reader who knows one
knows the other.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import websockets

from nox.core.logging import get_logger
from nox.plugins.reconnect import ReconnectBackoff

log = get_logger(__name__)

# ---- wire protocol (developers.home-assistant.io/docs/api/websocket) -----------------------------

TYPE_AUTH_REQUIRED = "auth_required"
TYPE_AUTH = "auth"  # noqa: S105 - a message type, not a credential
TYPE_AUTH_OK = "auth_ok"  # noqa: S105 - a message type, not a credential
TYPE_AUTH_INVALID = "auth_invalid"  # noqa: S105 - a message type, not a credential
TYPE_RESULT = "result"
TYPE_EVENT = "event"

#: Home Assistant closes the socket after `auth_invalid`, so the loop must not retry instantly.
AUTH_FAILURE_BACKOFF_S = 30.0


class HomeProtocolError(RuntimeError):
    """The peer did not follow the `auth_required` -> `auth` -> `auth_ok` handshake."""


class HomeAuthError(RuntimeError):
    """Home Assistant rejected the access token, or there is none to send."""


class HomeCommandError(RuntimeError):
    """A command came back with `success: false`."""

    def __init__(self, command: str, code: str, message: str) -> None:
        super().__init__(f"{command} failed ({code}): {message}")
        self.command = command
        self.code = code
        self.message = message


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]
ConnectedHook = Callable[[], Awaitable[None]]
DisconnectedHook = Callable[[str], Awaitable[None]]
TokenProvider = Callable[[], Awaitable[str | None]]


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> Any: ...


class HomeAssistantClient:
    """One authenticated Home Assistant WebSocket session, reconnecting until `stop`."""

    def __init__(
        self,
        url: str,
        *,
        token_provider: TokenProvider,
        on_event: EventHandler,
        on_connected: ConnectedHook | None = None,
        on_disconnected: DisconnectedHook | None = None,
        subscribe_event_types: tuple[str, ...] = ("state_changed",),
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        request_timeout_s: float = 10.0,
        connector: Callable[[str], Any] | None = None,
        authorize: Callable[[], None] | None = None,
    ) -> None:
        self.url = url
        self._token_provider = token_provider
        self._on_event = on_event
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._subscribe_event_types = subscribe_event_types
        self._min_backoff = min_backoff_s
        self._max_backoff = max_backoff_s
        self._request_timeout = request_timeout_s
        self._connector = connector or websockets.connect
        #: Called synchronously before every connection attempt: raw `websockets` connections
        #: bypass `PluginApi.http()`'s automatic guard, so this client enforces the plugin's scoped
        #: `EgressGuard` - and the privacy mode with it - itself.
        self._authorize = authorize
        self._ws: WebSocketLike | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: set[int] = set()
        self._next_id = 1
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.authenticated = False
        self.ha_version = ""
        self.last_error = ""
        self.backoff_s = min_backoff_s

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="home-ws-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001 - best-effort close during shutdown
                log.debug("home.ws_close_failed", error=str(exc))
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.connected = False
        self.authenticated = False

    async def _run(self) -> None:
        backoff = ReconnectBackoff("home", min_s=self._min_backoff, max_s=self._max_backoff)
        while not self._stop.is_set():
            auth_failed = False
            try:
                await self._connect_once()
            except HomeAuthError as exc:
                auth_failed = True
                backoff.failed(exc)
            except Exception as exc:  # noqa: BLE001 - the reconnect loop must never die
                backoff.failed(exc)
            else:
                backoff.succeeded()
            self.last_error = backoff.last_error
            was_connected = self.connected
            self.connected = False
            self.authenticated = False
            self._fail_pending(self.last_error or "disconnected")
            if was_connected and self._on_disconnected is not None:
                await self._on_disconnected(self.last_error)
            if self._stop.is_set():
                return
            if auth_failed:
                # A rejected token will be rejected again in a second; retrying at full speed only
                # fills Home Assistant's log with failed logins.
                backoff.current_s = max(backoff.current_s, self._auth_backoff())
            self.backoff_s = backoff.current_s
            if await backoff.sleep(self._stop):
                return

    def _auth_backoff(self) -> float:
        return min(AUTH_FAILURE_BACKOFF_S, max(self._max_backoff, self._min_backoff))

    async def _connect_once(self) -> None:
        if self._authorize is not None:
            self._authorize()
        async with self._connector(self.url) as ws:
            self._ws = ws
            await self._authenticate(ws)
            self.connected = True
            self.authenticated = True
            self.last_error = ""
            await self._subscribe(ws)
            if self._on_connected is not None:
                await self._on_connected()
            async for raw in ws:
                await self._dispatch(json.loads(raw))

    async def _authenticate(self, ws: WebSocketLike) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("type") != TYPE_AUTH_REQUIRED:
            raise HomeProtocolError(f"expected auth_required, got {hello.get('type')!r}")
        token = await self._token_provider()
        if not token:
            raise HomeAuthError(
                "no access token: store one with `nox secrets set nox/home/access_token`"
            )
        await ws.send(json.dumps({"type": TYPE_AUTH, "access_token": token}))
        reply = json.loads(await ws.recv())
        kind = reply.get("type")
        if kind == TYPE_AUTH_INVALID:
            raise HomeAuthError(
                "Home Assistant rejected the access token "
                "(create a new long-lived token and store it again)"
            )
        if kind != TYPE_AUTH_OK:
            raise HomeProtocolError(f"expected auth_ok, got {kind!r}")
        self.ha_version = str(reply.get("ha_version", ""))

    async def _subscribe(self, ws: WebSocketLike) -> None:
        """Subscribe to the configured event types on a fresh connection.

        Sent without awaiting the results: the reply arrives on the same read loop that has not
        started yet, so awaiting here would deadlock. A failed subscription surfaces as a
        `subscription failed` log line and an empty event stream, never as a silent success.
        """
        self._subscriptions.clear()
        for event_type in self._subscribe_event_types:
            command_id = self._take_id()
            self._subscriptions.add(command_id)
            await ws.send(
                json.dumps({"id": command_id, "type": "subscribe_events", "event_type": event_type})
            )

    def _take_id(self) -> int:
        command_id = self._next_id
        self._next_id += 1
        return command_id

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        kind = message.get("type")
        if kind == TYPE_RESULT:
            self._complete(message)
        elif kind == TYPE_EVENT:
            event = message.get("event") or {}
            result = self._on_event(str(event.get("event_type", "")), dict(event.get("data") or {}))
            if result is not None:
                await result

    def _complete(self, message: Mapping[str, Any]) -> None:
        command_id = int(message.get("id", 0))
        future = self._pending.pop(command_id, None)
        if future is None:
            if command_id in self._subscriptions and not message.get("success", True):
                error = message.get("error") or {}
                log.warning("home.subscription_failed", error=str(error.get("message", "")))
            return
        if future.done():
            return
        if message.get("success"):
            future.set_result(message.get("result"))
            return
        error = message.get("error") or {}
        future.set_exception(
            HomeCommandError(
                str(message.get("type", "command")),
                str(error.get("code", "unknown")),
                str(error.get("message", "")),
            )
        )

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self._pending.clear()

    # -- commands -------------------------------------------------------------------------------

    async def command(
        self,
        command_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Send one command and await its result; raises `HomeCommandError` on `success: false`."""
        if self._ws is None or not self.authenticated:
            raise ConnectionError(
                f"not connected to Home Assistant ({self.last_error or 'no session'})"
            )
        command_id = self._take_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[command_id] = future
        frame: dict[str, Any] = {"id": command_id, "type": command_type}
        if payload:
            frame.update(dict(payload))
        await self._ws.send(json.dumps(frame))
        try:
            return await asyncio.wait_for(future, timeout=timeout or self._request_timeout)
        finally:
            self._pending.pop(command_id, None)

    async def get_states(self) -> list[dict[str, Any]]:
        result = await self.command("get_states")
        return [dict(row) for row in result or [] if isinstance(row, Mapping)]

    async def registry(self, name: str) -> list[dict[str, Any]]:
        """`config/<name>_registry/list`. Needs an admin token; callers degrade when it fails."""
        result = await self.command(f"config/{name}_registry/list")
        return [dict(row) for row in result or [] if isinstance(row, Mapping)]

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_ids: list[str],
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "domain": domain,
            "service": service,
            "target": {"entity_id": list(entity_ids)},
        }
        if data:
            payload["service_data"] = dict(data)
        return await self.command("call_service", payload)
