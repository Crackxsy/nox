"""Bare protocol clients for testing the fakes themselves.

The fakes are only worth something if they behave like the real services, and the way to check
that without circular reasoning is to talk to them with a client that shares no code with Nox's:
these two speak the protocols with nothing but `websockets` and `asyncio` streams, one message or
line at a time, so a test can send exactly what the specification allows (or forbids).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import websockets

from .fake_obs.protocol import OP_HELLO, OP_IDENTIFIED, OP_IDENTIFY, OP_REQUEST, auth_string

#: How long a raw read waits before the test fails; nothing in a healthy run comes near it.
READ_TIMEOUT_S = 5.0


class RawObsClient:
    """One obs-websocket connection, driven by hand."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.ws: Any = None
        self.hello: dict[str, Any] = {}
        #: Events that arrived while `request` waited for its answer; `events` hands them out.
        self._events: list[dict[str, Any]] = []

    async def __aenter__(self) -> RawObsClient:
        self.ws = await websockets.connect(self.url)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.ws.close()

    async def receive(self) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), READ_TIMEOUT_S)
        message: dict[str, Any] = json.loads(raw)
        return message

    async def send(self, op: int, data: dict[str, Any]) -> None:
        await self.ws.send(json.dumps({"op": op, "d": data}))

    async def read_hello(self) -> dict[str, Any]:
        message = await self.receive()
        assert message["op"] == OP_HELLO
        self.hello = message["d"]
        return self.hello

    async def identify(
        self,
        *,
        password: str | None = None,
        rpc_version: int = 1,
        event_subscriptions: int | None = None,
    ) -> dict[str, Any]:
        """Hello, Identify (answering the challenge with `password`), and the reply."""
        hello = await self.read_hello()
        data: dict[str, Any] = {"rpcVersion": rpc_version}
        if event_subscriptions is not None:
            data["eventSubscriptions"] = event_subscriptions
        challenge = hello.get("authentication")
        if challenge and password is not None:
            data["authentication"] = auth_string(
                password, challenge["salt"], challenge["challenge"]
            )
        await self.send(OP_IDENTIFY, data)
        reply = await self.receive()
        assert reply["op"] == OP_IDENTIFIED, reply
        return reply["d"]

    async def request(
        self, request_type: str, request_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One request and its `RequestResponse` (events that arrive first are kept for
        `events`)."""
        request_id = f"raw-{request_type}"
        payload: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if request_data is not None:
            payload["requestData"] = request_data
        await self.send(OP_REQUEST, payload)
        while True:
            message = await self.receive()
            if message["op"] == 5:
                self._events.append(message["d"])
            elif message["op"] == 7 and message["d"]["requestId"] == request_id:
                response: dict[str, Any] = message["d"]
                return response

    async def events(self, count: int) -> list[dict[str, Any]]:
        """The next `count` `Event` messages (`d` of each), in order."""
        while len(self._events) < count:
            message = await self.receive()
            if message["op"] == 5:
                self._events.append(message["d"])
        found, self._events = self._events[:count], self._events[count:]
        return found

    async def close_code(self) -> int:
        """Wait for the server to close the connection; its close code."""
        await asyncio.wait_for(self.ws.wait_closed(), READ_TIMEOUT_S)
        return int(self.ws.close_code)


class RawIrcClient:
    """One IRC connection, line by line."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.reader: asyncio.StreamReader
        self.writer: asyncio.StreamWriter

    async def __aenter__(self) -> RawIrcClient:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def send(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode("utf-8"))
        await self.writer.drain()

    async def read_line(self) -> str:
        raw = await asyncio.wait_for(self.reader.readline(), READ_TIMEOUT_S)
        return raw.decode("utf-8").rstrip("\r\n")

    async def read_until(self, stop: Callable[[str], bool]) -> list[str]:
        """Lines up to and including the first one `stop` accepts."""
        lines: list[str] = []
        while True:
            line = await self.read_line()
            lines.append(line)
            if stop(line):
                return lines

    async def read_to_eof(self) -> list[str]:
        """Every line the server still sends until it closes the connection."""
        raw = await asyncio.wait_for(self.reader.read(), READ_TIMEOUT_S)
        return raw.decode("utf-8").splitlines()

    async def login(self, nick: str = "noxbot", token: str = "oauth:test-only") -> list[str]:
        """PASS, NICK, CAP REQ and JOIN as the bot sends them; every line up to the end of the
        names list."""
        await self.send(f"PASS {token}")
        await self.send(f"NICK {nick}")
        await self.send("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
        await self.send("JOIN #examplechannel")
        return await self.read_until(lambda line: " 366 " in line)
