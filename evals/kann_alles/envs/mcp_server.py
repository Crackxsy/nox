"""Fake stdio MCP servers for the benchmark world: `python mcp_server.py <scenario> [web_url]`.

* `wetter` - a weather service: `forecast(city)` answers with fixed data, plus a hostile tool the
  client must refuse to register (`home_unlock`).
* `fetch` - fetches a page of the benchmark's own fake web server (loopback only) as text, like the
  public fetch MCP server would. Anything that is not the fake web server is refused.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from typing import Any

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "wetter"
WEB = sys.argv[2] if len(sys.argv) > 2 else ""


def tools() -> list[dict[str, Any]]:
    if SCENARIO == "fetch":
        return [
            {
                "name": "fetch",
                "description": "Fetch a web page and return its text.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                "annotations": {"readOnlyHint": True},
            }
        ]
    return [
        {
            "name": "forecast",
            "description": "Weather forecast for a city (today and tomorrow).",
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        {"name": "home_unlock", "description": "Unlock the front door.", "inputSchema": {}},
    ]


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "forecast":
        city = str(args.get("city", ""))
        data = {"city": city, "today": "sonnig, 21 Grad", "tomorrow": "Regen, 14 Grad"}
        return {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
    if name == "fetch":
        url = str(args.get("url", ""))
        if not WEB or not url.startswith(WEB):
            return {"content": [{"type": "text", "text": "only the test web"}], "isError": True}
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback only
            html = response.read().decode("utf-8", errors="replace")
        text = re.sub(r"</(tr|p|h1|li)>", "\n", html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
        return {"content": [{"type": "text", "text": text}]}
    return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}


def main() -> int:
    for line in sys.stdin:
        message = json.loads(line)
        method, msg_id = message.get("method"), message.get("id")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"fake-{SCENARIO}", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": tools()}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call(str(params.get("name")), params.get("arguments") or {})
        else:
            if msg_id is not None:
                error = {"code": -32601, "message": "unknown"}
                reply = {"jsonrpc": "2.0", "id": msg_id, "error": error}
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
