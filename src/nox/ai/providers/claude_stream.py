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
    #: On `message_start`: the prompt tokens of that model step, by how they are billed.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    #: On `rate_limit`: the window that refused the request (`five_hour`, `seven_day`, ...) and when
    #: it resets (Unix seconds). Set only for a refusal, never for an allowed or warning status.
    limit_kind: str = ""
    limit_resets_at: float | None = None

    @property
    def refused(self) -> bool:
        """A `rate_limit` item that says a plan limit refused the request."""
        return self.kind == "rate_limit" and self.text == "rejected"


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
            return self._rate_limit(data.get("rate_limit_info") or {})
        if kind == "result":
            return self._result(data, sid)
        return StreamItem(kind="other")

    @staticmethod
    def _rate_limit(info: dict[str, Any]) -> StreamItem:
        """The status of the plan limits. Only `rejected` carries a refusal: the event itself, or
        the window inside `unifiedWindows` that refused (the one that resets last when several
        did, because the request may not go through before all of them have reset)."""
        status = str(info.get("status", ""))
        item = StreamItem(kind="rate_limit", text=status)
        refusals: list[tuple[str, float | None]] = []
        if status == "rejected":
            refusals.append((str(info.get("rateLimitType", "")), _as_float(info.get("resetsAt"))))
        windows = info.get("unifiedWindows")
        for name, window in windows.items() if isinstance(windows, dict) else ():
            if isinstance(window, dict) and window.get("status") == "rejected":
                refusals.append((str(name), _as_float(window.get("resetsAt"))))
        if not refusals:
            return item
        kind, resets_at = max(refusals, key=lambda refusal: refusal[1] or 0.0)
        return item.model_copy(
            update={"text": "rejected", "limit_kind": kind, "limit_resets_at": resets_at}
        )

    def _stream_event(self, event: dict[str, Any], sid: str) -> StreamItem:
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self.model = str(message.get("model", "") or self.model)
            usage = message.get("usage") or {}
            return StreamItem(
                kind="status",
                text="message_start",
                model=self.model,
                tokens_in=_as_int(usage.get("input_tokens")),
                cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
                cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
                tokens_out=_as_int(usage.get("output_tokens")),
            )
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
