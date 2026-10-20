"""Spike helper: the smallest MCP stdio server that answers `tools/list` and `tools/call`.

Two tools: `echo` (returns its text, logs the call) and `slow` (sleeps `seconds`, then answers).
Every call is appended to the file named by `SPIKE_LOG` so the driver can prove a call arrived.
"""

from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back. Use it when asked to echo something.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "slow",
        "description": "Wait the given number of seconds, then answer 'slow done'.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
        },
    },
]


def _log(entry: dict[str, object]) -> None:
    path = os.environ.get("SPIKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), **entry}) + "\n")


def _reply(msg_id: object, result: dict[str, object]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        _log({"method": method})
        if method == "initialize":
            _reply(
                msg_id,
                {
                    "protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "spike", "version": "0"},
                },
            )
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            _log({"call": name, "args": args})
            if name == "slow":
                time.sleep(float(args.get("seconds", 1)))
                text = "slow done"
            else:
                text = f"echo: {args.get('text', '')}"
            _log({"answered": name})
            _reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        elif msg_id is not None:
            _reply(msg_id, {})


if __name__ == "__main__":
    main()
