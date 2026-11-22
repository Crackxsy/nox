"""The server half of the fake OBS: the WebSocket handshake, request dispatch, events and the
failures a test can script."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

from .protocol import (
    CLOSE_ALREADY_IDENTIFIED,
    CLOSE_AUTHENTICATION_FAILED,
    CLOSE_MESSAGE_DECODE_ERROR,
    CLOSE_NOT_IDENTIFIED,
    CLOSE_UNKNOWN_OP_CODE,
    CLOSE_UNSUPPORTED_RPC_VERSION,
    EVENT_CATEGORY,
    OBS_VERSION,
    OBS_WEBSOCKET_VERSION,
    OP_EVENT,
    OP_HELLO,
    OP_IDENTIFIED,
    OP_IDENTIFY,
    OP_REQUEST,
    OP_REQUEST_RESPONSE,
    RPC_VERSION,
    SUB_ALL,
    ObsRequestError,
    Status,
    auth_string,
)
from .requests import Events, ObsRequests
from .state import ObsState


@dataclass
class RecordedRequest:
    """One request a client sent, with the connection (1, 2, ...) it came in on."""

    request_type: str
    data: dict[str, Any]
    connection: int


@dataclass
class IdentifyRecord:
    """One successful Identify: what the client announced about itself."""

    connection: int
    rpc_version: object
    event_subscriptions: int
    authenticated: bool


class _Client:
    def __init__(self, ws: ServerConnection, index: int) -> None:
        self.ws = ws
        self.index = index
        self.identified = False
        self.event_subscriptions = 0


class FakeObs:
    """One fake OBS instance on a loopback port the OS picked. `password=None`: no auth."""

    def __init__(
        self,
        *,
        password: str | None = None,
        scenes: tuple[str, ...] = ("Start", "Live"),
        current_scene: str | None = None,
    ) -> None:
        self.password = password
        self.state = ObsState()
        for name in scenes:
            self.state.scenes[name] = []
        self.state.current_scene = current_scene or (scenes[0] if scenes else "")
        #: Everything a client asked for, in order (after Identify).
        self.requests: list[RecordedRequest] = []
        self.identifies: list[IdentifyRecord] = []
        #: Connections that got as far as the WebSocket handshake.
        self.connection_count = 0
        self.port = 0
        self._server: Any = None
        self._clients: list[_Client] = []
        self._scripts: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._failures: dict[str, list[tuple[int, str]]] = {}
        self._ignored: set[str] = set()
        self._held: dict[str, asyncio.Event] = {}
        self._drop_on: set[str] = set()
        self._refuse: int = 0
        self._stall_hello = False
        self._arrived: dict[str, asyncio.Event] = {}
        self._requests = ObsRequests(self.state)
        self._tasks: set[asyncio.Task[None]] = set()

    # -- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> FakeObs:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    # -- what a test reads -------------------------------------------------------------------

    @property
    def identified_clients(self) -> int:
        return sum(1 for client in self._clients if client.identified)

    def requests_of(self, request_type: str) -> list[dict[str, Any]]:
        """The data of every request of this type, oldest first."""
        return [r.data for r in self.requests if r.request_type == request_type]

    def arrived(self, request_type: str) -> asyncio.Event:
        """Set once a request of this type has been received (also while it is held/ignored)."""
        return self._arrived.setdefault(request_type, asyncio.Event())

    # -- what a test scripts -------------------------------------------------------------------

    def script(
        self, request_type: str, handler: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        """Answer this request type with `handler(request_data)` instead of the built-in one."""
        self._scripts[request_type] = handler

    def fail_next(
        self,
        request_type: str,
        code: int = Status.REQUEST_PROCESSING_FAILED,
        comment: str = "scripted failure",
        *,
        times: int = 1,
    ) -> None:
        """Answer the next `times` requests of this type with `result: false`."""
        self._failures.setdefault(request_type, []).extend([(code, comment)] * times)

    def ignore(self, request_type: str) -> None:
        """Never answer this request type: the client's request timeout is what ends it."""
        self._ignored.add(request_type)

    def hold(self, request_type: str) -> asyncio.Event:
        """Receive this request type but answer only once the returned event is set."""
        gate = asyncio.Event()
        self._held[request_type] = gate
        return gate

    def drop_on(self, request_type: str) -> None:
        """Cut the connection (no close handshake) when this request type arrives."""
        self._drop_on.add(request_type)

    def refuse_connections(self, count: int = 1) -> None:
        """Accept the next `count` WebSocket connections and close them before Hello."""
        self._refuse = count

    def stall_hello(self, stalled: bool = True) -> None:
        """Accept connections but never send Hello (a client without a handshake timeout hangs)."""
        self._stall_hello = stalled

    async def drop_connections(self) -> None:
        """Cut every connection without a close handshake, like a crashed OBS or a pulled cable."""
        for client in list(self._clients):
            transport = getattr(client.ws, "transport", None)
            if transport is not None:
                transport.abort()
            else:  # pragma: no cover - a websockets without `.transport`
                await client.ws.close(code=1011)

    async def close_connections(self, code: int = 1001, reason: str = "") -> None:
        """Close every connection properly (OBS shutting down says 1001)."""
        for client in list(self._clients):
            await client.ws.close(code=code, reason=reason)

    async def emit(self, event_type: str, event_data: dict[str, Any] | None = None) -> None:
        """Send an event to every identified client subscribed to its category."""
        category = EVENT_CATEGORY.get(event_type)
        message = json.dumps(
            {
                "op": OP_EVENT,
                "d": {
                    "eventType": event_type,
                    "eventIntent": category or 0,
                    "eventData": event_data or {},
                },
            }
        )
        for client in list(self._clients):
            if not client.identified:
                continue
            if category is not None and not client.event_subscriptions & category:
                continue
            try:
                await client.ws.send(message)
            except websockets.ConnectionClosed:
                continue

    async def replay_events(self, messages: list[dict[str, Any]]) -> None:
        """Send recorded `Event` messages (`{"eventType": ..., "eventData": ...}`) in order."""
        for message in messages:
            await self.emit(str(message["eventType"]), dict(message.get("eventData", {})))

    # -- the connection -----------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        self.connection_count += 1
        client = _Client(ws, self.connection_count)
        self._clients.append(client)
        try:
            if self._refuse > 0:
                self._refuse -= 1
                await ws.close(code=1013, reason="try again later")
                return
            if self._stall_hello:
                await ws.wait_closed()
                return
            if not await self._handshake(client):
                return
            async for raw in ws:
                await self._on_message(client, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            if client in self._clients:
                self._clients.remove(client)

    async def _handshake(self, client: _Client) -> bool:
        ws = client.ws
        hello: dict[str, Any] = {
            "obsStudioVersion": OBS_VERSION,
            "obsWebSocketVersion": OBS_WEBSOCKET_VERSION,
            "rpcVersion": RPC_VERSION,
        }
        salt = challenge = ""
        if self.password is not None:
            salt = base64.b64encode(os.urandom(16)).decode()
            challenge = base64.b64encode(os.urandom(16)).decode()
            hello["authentication"] = {"challenge": challenge, "salt": salt}
        await ws.send(json.dumps({"op": OP_HELLO, "d": hello}))

        try:
            message = json.loads(await ws.recv())
        except ValueError:
            await ws.close(code=CLOSE_MESSAGE_DECODE_ERROR, reason="Not JSON")
            return False
        if message.get("op") != OP_IDENTIFY:
            await ws.close(code=CLOSE_NOT_IDENTIFIED, reason="Identify first")
            return False
        data = message.get("d", {}) or {}
        rpc_version = data.get("rpcVersion")
        if not isinstance(rpc_version, int) or rpc_version > RPC_VERSION:
            await ws.close(code=CLOSE_UNSUPPORTED_RPC_VERSION, reason="Unsupported RPC version")
            return False
        provided = data.get("authentication")
        if self.password is not None and provided != auth_string(self.password, salt, challenge):
            await ws.close(code=CLOSE_AUTHENTICATION_FAILED, reason="Authentication failed")
            return False
        client.identified = True
        client.event_subscriptions = int(data.get("eventSubscriptions", SUB_ALL))
        self.identifies.append(
            IdentifyRecord(
                client.index, rpc_version, client.event_subscriptions, self.password is not None
            )
        )
        await ws.send(json.dumps({"op": OP_IDENTIFIED, "d": {"negotiatedRpcVersion": RPC_VERSION}}))
        return True

    async def _on_message(self, client: _Client, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            await client.ws.close(code=CLOSE_MESSAGE_DECODE_ERROR, reason="Not JSON")
            return
        op = message.get("op")
        if op == OP_IDENTIFY:
            await client.ws.close(code=CLOSE_ALREADY_IDENTIFIED, reason="Already identified")
            return
        if op != OP_REQUEST:
            await client.ws.close(code=CLOSE_UNKNOWN_OP_CODE, reason="Unknown op code")
            return
        data = message.get("d", {}) or {}
        request_type = str(data.get("requestType", ""))
        request_id = str(data.get("requestId", ""))
        request_data = dict(data.get("requestData", {}) or {})
        self.requests.append(RecordedRequest(request_type, request_data, client.index))
        self.arrived(request_type).set()

        if request_type in self._drop_on:
            self._drop_on.discard(request_type)
            transport = getattr(client.ws, "transport", None)
            if transport is not None:
                transport.abort()
            return
        if request_type in self._ignored:
            return
        gate = self._held.get(request_type)
        if gate is not None:
            # Held on its own, so the connection keeps reading: a held request must not stop
            # the client's other requests from being answered.
            task = asyncio.create_task(self._answer_when_open(gate, client, message["d"]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        await self._answer(client, request_type, request_id, request_data)

    async def _answer_when_open(
        self, gate: asyncio.Event, client: _Client, data: dict[str, Any]
    ) -> None:
        await gate.wait()
        try:
            await self._answer(
                client,
                str(data.get("requestType", "")),
                str(data.get("requestId", "")),
                dict(data.get("requestData", {}) or {}),
            )
        except websockets.ConnectionClosed:
            pass

    async def _answer(
        self, client: _Client, request_type: str, request_id: str, request_data: dict[str, Any]
    ) -> None:
        status: dict[str, Any]
        response_data: dict[str, Any] = {}
        events: Events = []
        try:
            if not request_type:
                raise ObsRequestError(
                    Status.MISSING_REQUEST_TYPE, "Your request is missing its type."
                )
            queued = self._failures.get(request_type)
            if queued:
                code, comment = queued.pop(0)
                raise ObsRequestError(code, comment)
            scripted = self._scripts.get(request_type)
            if scripted is not None:
                response_data = scripted(request_data)
            else:
                handler = self._requests.handler(request_type)
                if handler is None:
                    raise ObsRequestError(
                        Status.UNKNOWN_REQUEST_TYPE, "Your request type is not recognized."
                    )
                response_data = handler(request_data, events) or {}
            status = {"result": True, "code": Status.SUCCESS}
        except ObsRequestError as failure:
            status = {"result": False, "code": failure.code, "comment": failure.comment}
            response_data = {}
            events = []
        await client.ws.send(
            json.dumps(
                {
                    "op": OP_REQUEST_RESPONSE,
                    "d": {
                        "requestType": request_type,
                        "requestId": request_id,
                        "requestStatus": status,
                        "responseData": response_data,
                    },
                }
            )
        )
        for event_type, event_data in events:
            await self.emit(event_type, event_data)
