"""A fake obs-websocket v5 server (`websockets.serve`) for the obs plugin's tests.

Speaks just enough of the real protocol.md to exercise the plugin against: Hello with an optional
SHA256 challenge/salt, Identify/Identified, Request/RequestResponse dispatch from a scriptable
table, and Event broadcast to every identified client. Shared by unit tests and
`tests/integration/test_obs_plugin.py`.

Extended for ST-15-02 (Spec v0.6 Clip Pipeline): default `GetReplayBufferStatus`/
`SaveReplayBuffer` responses plus `replay_buffer_saved()`, a thin wrapper over `broadcast_event`
that fires the `ReplayBufferSaved` event `obs.replay_buffer.save` waits on.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

OBS_WEBSOCKET_VERSION = "5.5.4"
RPC_VERSION = 1


def _expected_auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def default_scene_list(scenes: list[str], current: str) -> dict[str, Any]:
    return {
        "currentProgramSceneName": current,
        "scenes": [{"sceneName": name, "sceneIndex": i} for i, name in enumerate(scenes)],
    }


class FakeObsServer:
    """One fake OBS instance. `password=None` means "no auth required"."""

    def __init__(self, *, password: str | None = None) -> None:
        self.password = password
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {
            "GetSceneList": lambda _d: default_scene_list(["Start", "Live"], "Start"),
            "GetStreamStatus": lambda _d: {"outputActive": False},
            "GetRecordStatus": lambda _d: {"outputActive": False},
            "SetCurrentProgramScene": lambda _d: {},
            # ST-15-02: replay buffer defaults to "on" so a test only needs to override the one
            # case it wants (disabled, or a scripted failure); SaveReplayBuffer itself never
            # returns a path (that only comes via the ReplayBufferSaved event, see below).
            "GetReplayBufferStatus": lambda _d: {"outputActive": True},
            "SaveReplayBuffer": lambda _d: {},
        }
        self._clients: set[ServerConnection] = set()
        self._server: Any = None
        self.port = 0
        self.identify_count = 0

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def broadcast_event(self, event_type: str, event_data: dict[str, Any]) -> None:
        message = json.dumps(
            {"op": OP_EVENT, "d": {"eventType": event_type, "eventData": event_data}}
        )
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except websockets.ConnectionClosed:
                pass

    def set_response(self, request_type: str, value: Callable[[dict[str, Any]], Any] | Any) -> None:
        self.responses[request_type] = value

    async def replay_buffer_saved(self, path: str) -> None:
        """ST-15-02: fire the `ReplayBufferSaved` event `obs.replay_buffer.save` waits on."""
        await self.broadcast_event("ReplayBufferSaved", {"savedReplayPath": path})

    async def _handle(self, ws: ServerConnection) -> None:
        hello: dict[str, Any] = {
            "obsWebSocketVersion": OBS_WEBSOCKET_VERSION,
            "rpcVersion": RPC_VERSION,
        }
        salt = ""
        challenge = ""
        if self.password is not None:
            salt = base64.b64encode(os.urandom(16)).decode()
            challenge = base64.b64encode(os.urandom(16)).decode()
            hello["authentication"] = {"challenge": challenge, "salt": salt}
        await ws.send(json.dumps({"op": OP_HELLO, "d": hello}))

        try:
            raw = await ws.recv()
        except websockets.ConnectionClosed:
            return
        identify = json.loads(raw)
        if identify.get("op") != OP_IDENTIFY:
            await ws.close()
            return
        if self.password is not None:
            expected = _expected_auth(self.password, salt, challenge)
            provided = identify.get("d", {}).get("authentication")
            if provided != expected:
                await ws.close(code=4009, reason="Authentication failed")
                return
        self.identify_count += 1
        await ws.send(json.dumps({"op": OP_IDENTIFIED, "d": {"negotiatedRpcVersion": RPC_VERSION}}))
        self._clients.add(ws)
        try:
            async for raw_msg in ws:
                await self._on_message(ws, json.loads(raw_msg))
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    async def _on_message(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        if message.get("op") != OP_REQUEST:
            return
        d = message.get("d", {})
        request_type = str(d.get("requestType", ""))
        request_data = dict(d.get("requestData", {}) or {})
        request_id = str(d.get("requestId", uuid.uuid4().hex))
        self.requests.append((request_type, request_data))
        scripted = self.responses.get(request_type)
        if isinstance(scripted, Exception):
            status = {"result": False, "code": 500, "comment": str(scripted)}
            response_data: dict[str, Any] = {}
        else:
            response_data = scripted(request_data) if callable(scripted) else (scripted or {})
            status = {"result": True, "code": 100}
        await ws.send(
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
