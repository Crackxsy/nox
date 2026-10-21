"""`WebSocketTransport`: answers go to the MCP client, the peer's own messages to its handler, and
anything that breaks the framing ends the connection."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from nox.mcp.client import McpClient, McpClientError, WebSocketTransport


class FakeSocket:
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
        self.sent.append(json.loads(message))

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if item is None:
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.inbox.put_nowait(None)


async def test_answers_reach_the_client_and_peer_messages_the_handler() -> None:
    ws = FakeSocket()
    peer: list[dict[str, Any]] = []

    async def on_peer(message: dict[str, Any]) -> None:
        peer.append(message)

    client = McpClient(WebSocketTransport(ws, on_peer=on_peer), timeout_s=2)
    client.start()
    call = asyncio.create_task(client.call_tool("system.info", {}))
    await asyncio.sleep(0.01)
    request = ws.sent[-1]
    assert request["method"] == "tools/call"
    await ws.inbox.put(json.dumps({"jsonrpc": "2.0", "method": "nox/ping", "params": {}}))
    await ws.inbox.put(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "chat.send"}))
    await ws.inbox.put(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"ok": 1}}))
    assert await call == {"ok": 1}
    assert [m["method"] for m in peer] == ["nox/ping", "chat.send"]
    await client.close()


async def test_list_changed_goes_to_the_client() -> None:
    ws = FakeSocket()
    changed = asyncio.Event()
    peer: list[dict[str, Any]] = []

    async def on_peer(message: dict[str, Any]) -> None:
        peer.append(message)

    async def on_changed() -> None:
        changed.set()

    client = McpClient(WebSocketTransport(ws, on_peer=on_peer), on_tools_changed=on_changed)
    client.start()
    await ws.inbox.put(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}))
    await asyncio.wait_for(changed.wait(), 2)
    assert peer == []
    # The same name as a *request* is the peer's own message, not the client's notification.
    changed.clear()
    request = {"jsonrpc": "2.0", "id": 5, "method": "notifications/tools/list_changed"}
    await ws.inbox.put(json.dumps(request))
    for _ in range(100):
        if peer:
            break
        await asyncio.sleep(0.01)
    assert peer == [request] and not changed.is_set()
    await client.close()


@pytest.mark.parametrize(
    "frame",
    [
        b"\x00binary",
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode(),  # valid JSON, binary frame
        "not json",
        "[1, 2]",
        json.dumps({"id": 1, "result": {}}),
        "x" * 100,
    ],
)
async def test_a_broken_frame_ends_the_connection(frame: str | bytes) -> None:
    ws = FakeSocket()
    client = McpClient(WebSocketTransport(ws, max_message_bytes=64), timeout_s=2)
    client.start()
    await ws.inbox.put(frame)
    await asyncio.wait_for(client.wait_closed(), 2)
    assert client.closed


async def test_an_oversized_message_is_not_sent() -> None:
    transport = WebSocketTransport(FakeSocket(), max_message_bytes=32)
    with pytest.raises(McpClientError):
        await transport.send({"jsonrpc": "2.0", "method": "x", "params": {"p": "y" * 64}})


async def test_sending_on_a_closed_connection_is_a_client_error() -> None:
    ws = FakeSocket()
    ws.closed = True
    with pytest.raises(McpClientError):
        await WebSocketTransport(ws).send({"jsonrpc": "2.0", "method": "x"})
