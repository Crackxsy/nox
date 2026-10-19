"""`python -m nox.mcp.bridge`: the MCP server Claude Code starts, forwarding to the Nox core.

It holds no tool and decides nothing. MCP requests from stdin become hub requests under the IPC
role `mcp` (`nox.mcp.gate` answers them); the answers go back to stdout. Its credentials come from
the environment the core wrote into the MCP config: the hub URL, a one-time token bound to the role,
and its client id. The core pushes the token of each turn (`mcp.turn`) before the turn starts, and
every `tools/call` carries it; without one the core refuses the call.

The process ends when Claude Code closes stdin, or when the core drops the connection (a kill
switch, the end of the Claude Code session) - whichever comes first.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from typing import Any

from nox import __version__
from nox.ipc.client import InboundRequest, IpcClient
from nox.ipc.errors import IpcError
from nox.ipc.tokens import WORKER_TOKEN_ENV
from nox.mcp import protocol
from nox.mcp.protocol import CLIENT_ID_ENV, HUB_URL_ENV, MCP_ROLE

#: A call may wait for a confirmation dialog (60 s) and then run with its own timeout.
CALL_TIMEOUT_S = 200.0
LIST_TIMEOUT_S = 10.0
EXIT_CONFIG = 2
EXIT_HUB = 3


class Bridge:
    """MCP stdio on one side, the hub on the other. One instance per process."""

    def __init__(self, client: IpcClient, out: Any) -> None:
        self._client = client
        self._out = out
        self._token = ""
        self._write_lock = asyncio.Lock()
        client.handle("mcp.turn", self._on_turn)

    async def _on_turn(self, payload: dict[str, Any], _request: InboundRequest) -> dict[str, Any]:
        self._token = str(payload.get("token", ""))
        return {"ok": True}

    async def write(self, line: str) -> None:
        async with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    async def handle(self, message: Mapping[str, Any]) -> str | None:
        """The response line for one request, or `None` for a notification."""
        msg_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if msg_id is None:
            return None
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            return protocol.result(msg_id, protocol.initialize_result(requested, __version__))
        if method == "ping":
            return protocol.result(msg_id, {})
        if method == "tools/list":
            return await self._list(msg_id)
        if method == "tools/call":
            return await self._call(msg_id, params if isinstance(params, dict) else {})
        return protocol.error(msg_id, protocol.ERR_METHOD_NOT_FOUND, f"unknown method {method}")

    async def _list(self, msg_id: Any) -> str:
        try:
            answer = await self._client.request("mcp.tools.list", {}, timeout=LIST_TIMEOUT_S)
        except IpcError as exc:
            return protocol.error(msg_id, protocol.ERR_INTERNAL, exc.message)
        return protocol.result(msg_id, {"tools": answer.get("tools", [])})

    async def _call(self, msg_id: Any, params: Mapping[str, Any]) -> str:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return protocol.error(msg_id, protocol.ERR_INVALID_PARAMS, "name and arguments")
        if not self._token:
            return protocol.result(
                msg_id,
                protocol.tool_result("No turn is open; the call was refused.", is_error=True),
            )
        payload = {"token": self._token, "name": name, "arguments": arguments}
        try:
            answer = await self._client.request("mcp.tools.call", payload, timeout=CALL_TIMEOUT_S)
        except IpcError as exc:
            text = f"{name} was refused by Nox: {exc.message}"
            return protocol.result(msg_id, protocol.tool_result(text, is_error=True))
        ok = bool(answer.get("ok"))
        return protocol.result(
            msg_id, protocol.tool_result(str(answer.get("text", "")), is_error=not ok)
        )


async def _read_lines(queue: asyncio.Queue[str | None]) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await queue.put(None)
            return
        await queue.put(line)


async def serve(client: IpcClient, bridge: Bridge) -> int:
    lines: asyncio.Queue[str | None] = asyncio.Queue()
    reader = asyncio.create_task(_read_lines(lines))
    pending: set[asyncio.Task[None]] = set()

    async def answer(message: dict[str, Any]) -> None:
        reply = await bridge.handle(message)
        if reply is not None:
            await bridge.write(reply)

    try:
        while client.connected:
            line = await lines.get()
            if line is None:
                break
            message = protocol.parse_line(line)
            if message is None:
                await bridge.write(protocol.error(None, protocol.ERR_PARSE, "not JSON-RPC 2.0"))
                continue
            task = asyncio.create_task(answer(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        reader.cancel()
        for task in list(pending):
            task.cancel()
        await client.close()
    return 0


async def _main(env: Mapping[str, str]) -> int:
    url, token, client_id = env.get(HUB_URL_ENV), env.get(WORKER_TOKEN_ENV), env.get(CLIENT_ID_ENV)
    if not url or not token or not client_id:
        sys.stderr.write("nox.mcp.bridge: started without hub credentials\n")
        return EXIT_CONFIG
    client = IpcClient(url, token, MCP_ROLE, client_id, client_version=__version__)
    # Created before connecting: the core pushes the turn token as soon as the bridge is in.
    bridge = Bridge(client, sys.stdout)
    try:
        await client.connect()
    except IpcError as exc:
        sys.stderr.write(f"nox.mcp.bridge: cannot reach the core: {exc.message}\n")
        return EXIT_HUB
    return await serve(client, bridge)


def main() -> int:
    # The token is read once and removed, so nothing this process might start inherits it.
    env = dict(os.environ)
    os.environ.pop(WORKER_TOKEN_ENV, None)
    return asyncio.run(_main(env))


if __name__ == "__main__":
    sys.exit(main())
