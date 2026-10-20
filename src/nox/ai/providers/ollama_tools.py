"""Tool calling for Ollama: the native `tools` field, and the text-sentinel adapter for the rest.

Models that declare the `tools` capability (`/api/show`) get Nox's offer as `tools` and answer with
`message.tool_calls`; results go back as `role: tool` messages. Models without it get the offer as
text (`nox.ai.tooluse`) and ask with one `NOX_TOOL_CALL {...}` line; `SentinelSniffer` holds the
first characters back until it is clear whether an answer is prose or a directive, and turns a
directive into the same `ToolCallRequest` the native path produces - so the orchestrator never
sees, speaks or shows the protocol line. Names travel in wire form (`file-read`), see
`nox.tools.names`.
"""

from __future__ import annotations

import json
from typing import Any

from nox.ai.base import AiRequest, Message, ToolCallRequest
from nox.ai.tooluse import SENTINEL, decided_prose, parse_directive, render_offer
from nox.tools.names import from_wire, to_wire

__all__ = [
    "INVALID_DIRECTIVE",
    "SentinelSniffer",
    "native_messages",
    "native_tools",
    "parse_tool_calls",
    "sentinel_messages",
]

#: Name given to a directive that could not be parsed; the tool loop refuses it as not offered,
#: which tells the model its line was not a valid call.
INVALID_DIRECTIVE = "invalid_directive"


def native_tools(request: AiRequest) -> list[dict[str, Any]]:
    tools = []
    for tool in request.tools:
        description = tool.description
        if tool.asks_first:
            description += " (asks the user for confirmation first)"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": to_wire(tool.name),
                    "description": description,
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _system(request: AiRequest, extra: str = "") -> list[dict[str, Any]]:
    text = request.system_text()
    if extra:
        text = f"{text}\n\n{extra}" if text else extra
    return [{"role": "system", "content": text}] if text else []


def native_messages(request: AiRequest) -> list[dict[str, Any]]:
    out = _system(request)
    for message in request.messages:
        if message.role == "system":
            continue
        out.append(_native(message))
    return out


def _native(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "content": message.content, "tool_name": to_wire(message.tool_name)}
    entry: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {"function": {"name": to_wire(call.name), "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    return entry


def sentinel_messages(request: AiRequest) -> list[dict[str, Any]]:
    """The same conversation for a model without native tools: offer in the system message,
    calls as directive lines, results as user messages."""
    allowed = request.tools
    out = _system(request, render_offer(allowed) if allowed else "")
    for message in request.messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            out.append({"role": "user", "content": message.content})
            continue
        content = message.content
        if message.tool_calls:
            call = message.tool_calls[0]
            line = json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False)
            content = f"{SENTINEL} {line}"
        out.append({"role": message.role, "content": content})
    return out


def parse_tool_calls(message: dict[str, Any]) -> list[ToolCallRequest]:
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        calls.append(
            ToolCallRequest(
                id=str(raw.get("id") or f"call_{index}"),
                name=from_wire(str(function.get("name", ""))),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


class SentinelSniffer:
    """Streams prose through and catches a directive before anyone sees it."""

    def __init__(self, allowed: list[str]) -> None:
        self._allowed = allowed
        self._pending = ""
        self._decided: bool | None = None  # True prose, False directive
        self._directive = ""

    def feed(self, delta: str) -> str:
        """Text that may be shown and spoken now ("" while undecided or inside a directive)."""
        if self._decided is True:
            return delta
        if self._decided is False:
            self._directive += delta
            return ""
        self._pending += delta
        verdict = decided_prose(self._pending)
        if verdict is None:
            return ""
        self._decided = verdict
        held, self._pending = self._pending, ""
        if verdict:
            return held
        self._directive = held
        return ""

    def finish(self) -> tuple[str, list[ToolCallRequest]]:
        """At the end of the answer: text still held back, and the call if it was a directive."""
        if self._decided is None:
            held, self._pending = self._pending, ""
            if not held.lstrip().startswith(SENTINEL):
                return held, []
            self._directive = held
        if self._decided is True:
            return "", []
        parsed = parse_directive(self._directive, self._allowed)
        if parsed is None:
            return self._directive, []
        if isinstance(parsed, str):
            return "", [ToolCallRequest(id="call_0", name=INVALID_DIRECTIVE, arguments={})]
        return "", [ToolCallRequest(id="call_0", name=parsed.name, arguments=parsed.arguments)]
