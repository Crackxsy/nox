"""A real (tiny) MCP stdio server inside the real sandbox, spoken to through `SandboxLauncher`.

Headless: the server is a few lines of standard-library Python passed with `-c`, started by
`Sandbox.start` (AppContainer on Windows, bwrap on Linux) - no window, no network. Skipped where
no strong sandbox can be built.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nox.mcp.client import McpClient
from nox.mcp.launch import SandboxLauncher

pytestmark = pytest.mark.timeout(120)

#: Answers initialize, tools/list (one tool) and tools/call (echo), one JSON object per line.
SERVER = r"""
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "tiny", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo the text.",
                             "inputSchema": {"type": "object",
                                             "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        text = msg["params"]["arguments"].get("text", "")
        result = {"content": [{"type": "text", "text": "echo:" + text}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


def _strong_sandbox() -> object:
    try:
        import nox.sandbox as module

        sandbox = module.make_sandbox()
    except Exception as exc:  # noqa: BLE001 - no backend on this machine
        pytest.skip(f"no sandbox here: {exc}")
    if not sandbox.guarantees().strong:
        pytest.skip("only a weak sandbox here")
    return sandbox, module


async def test_a_stdio_server_runs_and_answers_inside_the_sandbox(tmp_path: Path) -> None:
    sandbox, module = _strong_sandbox()  # type: ignore[misc]
    launcher = SandboxLauncher(sandbox, module)
    launched = await launcher.launch(
        [sys.executable, "-I", "-c", SERVER], cwd=tmp_path / "unused", env={}, network=False
    )
    client = McpClient(launched.transport(), timeout_s=30.0)
    client.start()
    try:
        await client.initialize(timeout_s=60.0)
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        result = await client.call_tool("echo", {"text": "hallo"})
        assert "echo:hallo" in str(result)
    finally:
        await client.close()
        await launched.terminate()
    assert not (tmp_path / "unused").exists()  # nothing was created under the caller's folder
