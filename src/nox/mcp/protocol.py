"""The slice of the Model Context Protocol (stdio transport) the bridge speaks.

One JSON-RPC 2.0 message per line. The server side Nox needs is small: `initialize`, `ping`,
`tools/list`, `tools/call`, and the `notifications/tools/list_changed` notification. Everything
here is pure - parsing and building messages - so it can be tested without a process.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "CLIENT_ID_ENV",
    "ERR_INTERNAL",
    "ERR_INVALID_PARAMS",
    "ERR_INVALID_REQUEST",
    "ERR_METHOD_NOT_FOUND",
    "ERR_PARSE",
    "HUB_URL_ENV",
    "MCP_ROLE",
    "PROTOCOL_VERSION",
    "error",
    "initialize_result",
    "list_changed",
    "parse_line",
    "result",
    "tool_result",
]

PROTOCOL_VERSION = "2025-06-18"
#: IPC role of the bridge, and the environment the core hands it through the MCP config.
MCP_ROLE = "mcp"
HUB_URL_ENV = "NOX_HUB_URL"
CLIENT_ID_ENV = "NOX_MCP_CLIENT_ID"
SERVER_NAME = "nox"

ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


def parse_line(line: str) -> dict[str, Any] | None:
    """A JSON-RPC message, or `None` for a line that is not one (answered with a parse error)."""
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return None
    return message


def _dump(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def result(msg_id: Any, payload: dict[str, Any]) -> str:
    return _dump({"jsonrpc": "2.0", "id": msg_id, "result": payload})


def error(msg_id: Any, code: int, message: str) -> str:
    return _dump({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def list_changed() -> str:
    return _dump({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


def initialize_result(requested_version: object, server_version: str) -> dict[str, Any]:
    version = requested_version if isinstance(requested_version, str) else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        # The tool list is fixed for the life of a bridge: a different offer gets a new Claude
        # Code process (the offer is part of the session fingerprint), so no change is announced.
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": server_version},
    }


def tool_result(text: str, *, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}
