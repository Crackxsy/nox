"""Parsing Claude Code's `stream-json` output, shared by the one-shot and the session path."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

__all__ = ["ClaudeStreamParser", "StreamItem"]


class StreamItem(BaseModel):
    """One parsed NDJSON line of the ``stream-json`` output."""

    kind: Literal["init", "delta", "text", "tool", "result", "status", "rate_limit", "other"]
    text: str = ""
    model: str = ""
    session_id: str = ""
    is_error: bool = False
    error: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    stop_reason: str = ""
    terminal_reason: str = ""


class ClaudeStreamParser:
    """Stateful parser for ``claude -p --output-format stream-json`` lines.

    With ``--include-partial-messages`` text arrives as ``stream_event`` deltas and the following
    ``assistant`` line repeats it; the parser reports the repeat as ``text`` (not ``delta``) so the
    consumer never double-counts. Without partial messages, ``assistant`` text becomes the delta.
    """

    def __init__(self) -> None:
        self.saw_partial = False
        self.model = ""
        self.session_id = ""

    def feed(self, line: str) -> StreamItem | None:
        line = line.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return StreamItem(kind="other", text=line)
        if not isinstance(data, dict):
            return StreamItem(kind="other", text=line)
        kind = data.get("type")
        sid = str(data.get("session_id", "") or "")
        if sid:
            self.session_id = sid
        if kind == "system":
            if data.get("subtype") == "init":
                self.model = str(data.get("model", "") or "")
                return StreamItem(kind="init", model=self.model, session_id=sid)
            return StreamItem(kind="status", text=str(data.get("status", data.get("subtype", ""))))
        if kind == "stream_event":
            return self._stream_event(data.get("event") or {}, sid)
        if kind == "assistant":
            text = self._message_text(data.get("message") or {})
            if not text:
                return StreamItem(kind="other")
            return StreamItem(kind="text" if self.saw_partial else "delta", text=text)
        if kind == "rate_limit_event":
            info = data.get("rate_limit_info") or {}
            return StreamItem(kind="rate_limit", text=str(info.get("status", "")))
        if kind == "result":
            return self._result(data, sid)
        return StreamItem(kind="other")

    def _stream_event(self, event: dict[str, Any], sid: str) -> StreamItem:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self.model = str(message.get("model", "") or self.model)
            return StreamItem(kind="status", text="message_start", model=self.model)
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                return StreamItem(kind="tool", text=str(block.get("name", "")))
            return StreamItem(kind="status", text="content_block_start")
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                self.saw_partial = True
                return StreamItem(kind="delta", text=str(delta.get("text", "")))
            return StreamItem(kind="other")
        if etype == "message_delta":
            usage = event.get("usage") or {}
            delta = event.get("delta") or {}
            return StreamItem(
                kind="status",
                text="message_delta",
                stop_reason=str(delta.get("stop_reason", "") or ""),
                tokens_out=_as_int(usage.get("output_tokens")),
            )
        return StreamItem(kind="status", text=str(etype or ""))

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        parts = []
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)

    def _result(self, data: dict[str, Any], sid: str) -> StreamItem:
        usage = data.get("usage") or {}
        tokens_in = _sum_ints(
            usage.get("input_tokens"),
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_read_input_tokens"),
        )
        is_error = bool(data.get("is_error")) or str(data.get("subtype", "")).startswith("error")
        result_text = str(data.get("result", "") or "")
        return StreamItem(
            kind="result",
            text=result_text,
            model=self.model,
            session_id=sid,
            is_error=is_error,
            error=result_text if is_error else "",
            tokens_in=tokens_in,
            tokens_out=_as_int(usage.get("output_tokens")),
            cost_usd=_as_float(data.get("total_cost_usd")),
            duration_ms=_as_int(data.get("duration_ms")),
            duration_api_ms=_as_int(data.get("duration_api_ms")),
            stop_reason=str(data.get("stop_reason", "") or ""),
            terminal_reason=str(data.get("terminal_reason", "") or ""),
        )


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _sum_ints(*values: object) -> int | None:
    ints = [v for v in (_as_int(x) for x in values) if v is not None]
    return sum(ints) if ints else None
