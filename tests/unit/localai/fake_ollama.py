"""A fake Ollama server for the setup flow, on `httpx.MockTransport` - nothing leaves the process.

Same pattern as `tests/integration/test_ollama_native_tools.py`, extended by the endpoints the
setup uses: `/api/version`, `/api/tags`, `/api/pull` (streamed NDJSON progress), `/api/show`, and
`/api/chat` both as one JSON answer (`stream: false`) and as NDJSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx


def pull_lines(digest: str = "sha256:abc", total: int = 1000) -> list[dict[str, Any]]:
    return [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "digest": digest, "total": total, "completed": 0},
        {"status": "pulling abc", "digest": digest, "total": total, "completed": total // 2},
        {"status": "pulling abc", "digest": digest, "total": total, "completed": total},
        {"status": "verifying sha256 digest"},
        {"status": "writing manifest"},
        {"status": "success"},
    ]


def native_call(name: str) -> list[dict[str, Any]]:
    call = {"function": {"name": name, "arguments": {}}}
    return [
        {"message": {"content": "", "tool_calls": [call]}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]


def prose(text: str) -> list[dict[str, Any]]:
    return [
        {"message": {"content": text}, "done": False},
        {"message": {"content": ""}, "done": True},
    ]


class FakeOllama:
    def __init__(
        self,
        *,
        reachable: bool = True,
        models: list[str] | None = None,
        tools_capability: bool = True,
        pull: list[dict[str, Any]] | None = None,
        text_answer: str = "Hallo!",
        tool_answer: list[dict[str, Any]] | None = None,
    ) -> None:
        self.reachable = reachable
        self.models = list(models or [])
        self.capabilities = ["completion", "tools"] if tools_capability else ["completion"]
        self.pull = pull if pull is not None else pull_lines()
        self.text_answer = text_answer
        self.tool_answer = (
            tool_answer if tool_answer is not None else native_call("desktop-windows")
        )
        self.requests: list[tuple[str, str]] = []
        self.pull_bodies: list[dict[str, Any]] = []
        self.chat_bodies: list[dict[str, Any]] = []

    def paths(self) -> list[str]:
        return [path for _method, path in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if not self.reachable:
            raise httpx.ConnectError("connection refused", request=request)
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/pull":
            body = json.loads(request.content)
            self.pull_bodies.append(body)
            if any(line.get("status") == "success" for line in self.pull):
                self.models.append(body["model"])
            return httpx.Response(200, content=_ndjson(self.pull))
        if path == "/api/show":
            return httpx.Response(200, json={"capabilities": self.capabilities})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.chat_bodies.append(body)
            if not body.get("stream", True):
                return httpx.Response(
                    200, json={"message": {"content": self.text_answer}, "done": True}
                )
            return httpx.Response(200, content=_ndjson(self.tool_answer))
        return httpx.Response(404)

    def factory(self) -> Callable[..., httpx.AsyncClient]:
        def make(**_kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

        return make


def _ndjson(lines: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
