"""Nox as an MCP *client*: talking to someone else's server, over stdio or streamable HTTP.

`nox.mcp.bridge` is the other direction (Nox's tools served to Claude Code). This module is the
small client half of the Model Context Protocol that Nox needs to *use* a server: `initialize`,
`tools/list` (with pagination), `tools/call`, and the `notifications/tools/list_changed`
notification. Nothing else is offered to the server - no sampling, no roots, no elicitation - so a
request from the server for any of those is answered with "method not found". A server cannot make
Nox run a model, read a folder or ask the user anything.

Everything that arrives is bounded: a message larger than `MAX_MESSAGE_BYTES` ends the connection,
a list of more than `max_tools` tools is cut, and a call that does not answer in time is an error.
What a server *says* is not trusted here either; `nox.mcp.servers` decides what is shown to whom.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from nox.core.logging import get_logger
from nox.mcp.protocol import PROTOCOL_VERSION

log = get_logger(__name__)

__all__ = [
    "MAX_MESSAGE_BYTES",
    "HttpTransport",
    "McpClient",
    "McpClientError",
    "RemoteTool",
    "StdioTransport",
    "Transport",
]

#: Largest single message accepted from a server. A tool list or a result larger than this is
#: either broken or hostile, and both end the connection.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
CLIENT_NAME = "nox"
ERR_METHOD_NOT_FOUND = -32601


class McpClientError(RuntimeError):
    """The server broke the protocol, answered with an error, went away or took too long."""


class Transport(Protocol):
    """One connection to a server: send a JSON-RPC message, iterate over what comes back."""

    async def send(self, message: dict[str, Any]) -> None: ...
    def messages(self) -> AsyncIterator[dict[str, Any]]: ...
    async def close(self) -> None: ...


# ---- stdio ---------------------------------------------------------------------------------------


class StdioTransport:
    """Newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._writer.write(data.encode("utf-8"))
        await self._writer.drain()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                line = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                if exc.partial.strip():
                    log.info("mcp.client.partial_line_at_eof", size=len(exc.partial))
                return
            except asyncio.LimitOverrunError as exc:
                raise McpClientError("the server sent a message larger than allowed") from exc
            if len(line) > MAX_MESSAGE_BYTES:
                raise McpClientError("the server sent a message larger than allowed")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # Servers print banners on stdout more often than they should. Ignored, not fatal.
                log.info("mcp.client.non_json_line", size=len(text))
                continue
            if isinstance(message, dict):
                yield message

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.close()


# ---- streamable HTTP ----------------------------------------------------------------------------


class HttpTransport:
    """MCP streamable HTTP: every message is a POST; answers come as JSON or as an SSE stream.

    The client comes from the core's egress guard, so the host is checked against the privacy
    mode and the profile on every request, like every other outbound request Nox makes.
    """

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._url = url
        self._client = client
        self._headers = dict(headers or {})
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    def _request_headers(self) -> dict[str, str]:
        headers = {
            **self._headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    async def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        async with self._client.stream(
            "POST", self._url, content=body, headers=self._request_headers()
        ) as response:
            session = response.headers.get("mcp-session-id")
            if session:
                self._session_id = session
            if response.status_code == 202:
                return
            if response.status_code >= 400:
                raise McpClientError(f"the server answered HTTP {response.status_code}")
            kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind == "text/event-stream":
                await self._read_events(response)
                return
            raw = await self._read_capped(response)
            if raw.strip():
                self._enqueue(raw)

    @staticmethod
    async def _read_capped(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_MESSAGE_BYTES:
                raise McpClientError("the server sent a message larger than allowed")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _read_events(self, response: httpx.Response) -> None:
        data: list[str] = []
        size = 0
        async for line in response.aiter_lines():
            size += len(line)
            if size > MAX_MESSAGE_BYTES:
                raise McpClientError("the server sent a message larger than allowed")
            if not line:
                if data:
                    self._enqueue("\n".join(data).encode("utf-8"))
                    data = []
                    size = 0
                continue
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
        if data:
            self._enqueue("\n".join(data).encode("utf-8"))

    def _enqueue(self, raw: bytes) -> None:
        try:
            decoded = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise McpClientError("the server sent something that is not JSON") from exc
        items = decoded if isinstance(decoded, list) else [decoded]
        for item in items:
            if isinstance(item, dict):
                self._queue.put_nowait(item)

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self._queue.put_nowait(None)
        with contextlib.suppress(Exception):
            await self._client.aclose()


# ---- the client ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """A tool as the server describes it. Every field is the server's word, not Nox's."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    read_only_hint: bool | None = None


ListChanged = Callable[[], Awaitable[None]]


class McpClient:
    """JSON-RPC request/response over one transport, with a reader task demultiplexing answers."""

    def __init__(
        self,
        transport: Transport,
        *,
        timeout_s: float = 60.0,
        on_tools_changed: ListChanged | None = None,
        client_version: str = "0",
    ) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._on_tools_changed = on_tools_changed
        self._client_version = client_version
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self.server_info: dict[str, Any] = {}

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read(), name="nox-mcp-client-reader")

    async def _read(self) -> None:
        try:
            async for message in self._transport.messages():
                await self._dispatch(message)
        except McpClientError as exc:
            log.warning("mcp.client.connection_broken", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a broken stream ends this connection only
            log.warning("mcp.client.reader_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpClientError("the server closed the connection"))
            self._pending.clear()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        if isinstance(method, str):
            if msg_id is not None:
                # A request *from* the server. Nox offers nothing to servers.
                await self._safe_send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": ERR_METHOD_NOT_FOUND, "message": "not supported"},
                    }
                )
                return
            if method == "notifications/tools/list_changed" and self._on_tools_changed:
                asyncio.ensure_future(self._on_tools_changed())
            return
        if not isinstance(msg_id, int):
            return
        future = self._pending.pop(msg_id, None)
        if future is not None and not future.done():
            future.set_result(message)

    async def _safe_send(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._transport.send(message)

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        if self.closed:
            raise McpClientError("the connection is closed")
        self.start()
        msg_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        wait = self._timeout_s if timeout_s is None else timeout_s
        try:
            await asyncio.wait_for(self._transport.send(message), wait)
            answer = await asyncio.wait_for(future, wait)
        except TimeoutError as exc:
            raise McpClientError(f"{method}: no answer within {wait:g} s") from exc
        except httpx.HTTPError as exc:
            raise McpClientError(f"{method}: {type(exc).__name__}") from exc
        finally:
            self._pending.pop(msg_id, None)
        error = answer.get("error")
        if isinstance(error, dict):
            raise McpClientError(f"{method}: {str(error.get('message', 'error'))[:200]}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise McpClientError(f"{method}: the answer has no result")
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._transport.send(message)

    async def initialize(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": self._client_version},
            },
            timeout_s=timeout_s,
        )
        version = result.get("protocolVersion")
        if isinstance(self._transport, HttpTransport) and isinstance(version, str):
            self._transport.set_protocol_version(version)
        info = result.get("serverInfo")
        self.server_info = dict(info) if isinstance(info, dict) else {}
        await self.notify("notifications/initialized")
        return result

    async def list_tools(self, *, max_tools: int = 64, max_pages: int = 10) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        cursor: str | None = None
        for _ in range(max_pages):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            for raw in result.get("tools") or []:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    continue
                schema = raw.get("inputSchema")
                annotations = raw.get("annotations")
                hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
                tools.append(
                    RemoteTool(
                        name=str(raw["name"]),
                        description=str(raw.get("description") or ""),
                        input_schema=dict(schema) if isinstance(schema, dict) else {},
                        read_only_hint=hint if isinstance(hint, bool) else None,
                    )
                )
                if len(tools) >= max_tools:
                    return tools
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        return await self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s
        )

    async def close(self) -> None:
        await self._transport.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        self._closed.set()
