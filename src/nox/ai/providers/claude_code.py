"""ClaudeCodeProvider: Claude Code CLI as text provider via ``claude -p`` (ADR-008, FR-6.3, SP-01).

Verified against Claude Code 2.1.266 on this machine:
``claude -p --output-format stream-json --verbose --include-partial-messages`` reads the prompt from
stdin and writes NDJSON lines: ``system/init``, ``stream_event`` (Anthropic message stream events,
``content_block_delta`` carries text), ``assistant`` (complete message), ``rate_limit_event`` and a
final ``result`` line with ``usage``, ``total_cost_usd``, ``duration_ms`` and ``is_error``.
``--safe-mode`` disables the user's hooks/plugins/MCP for Nox requests but keeps the CLI login;
``--bare`` is NOT used because it ignores OAuth logins. Tools are disabled (``--tools ""``); agentic
coding sessions (FR-11.10) are a separate wrapper, not this provider. No secrets are passed: the CLI
authenticates itself. The child process is killed when the request is cancelled (kill switch).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from nox.ai._log import get_logger
from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, Message, ProviderInfo
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.core.events import HealthStatus

PROVIDER_ID = "claude_code"
STDOUT_LINE_LIMIT = 4 * 1024 * 1024
STDERR_CAPTURE_BYTES = 16 * 1024

log = get_logger(__name__)


class StreamItem(BaseModel):
    """One parsed NDJSON line of the ``stream-json`` output."""

    kind: Literal["init", "delta", "text", "result", "status", "rate_limit", "other"]
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


def render_prompt(request: AiRequest) -> tuple[str, str]:
    """Split messages into (system_prompt, user_prompt). Multi-turn history is rendered as a
    transcript because ``claude -p`` takes exactly one prompt."""
    system_parts = [m.content for m in request.messages if m.role == "system"]
    turns = [m for m in request.messages if m.role != "system"]
    if not turns:
        return "\n\n".join(system_parts), ""
    if len(turns) == 1:
        return "\n\n".join(system_parts), turns[0].content
    history = turns[:-1]
    lines = ["Previous conversation:"]
    for m in history:
        speaker = "User" if m.role == "user" else "Nox"
        lines.append(f"{speaker}: {m.content}")
    lines.append("")
    lines.append(f"Current message from the user:\n{turns[-1].content}")
    return "\n\n".join(system_parts), "\n".join(lines)


class ClaudeCodeProvider:
    """Runs the Claude Code CLI as a subprocess per request. Cloud provider (``local=False``)."""

    def __init__(
        self,
        config: ClaudeCodeConfig,
        *,
        env: Mapping[str, str] | None = None,
        command_override: Sequence[str] | None = None,
        roles: list[AiRole] | None = None,
    ) -> None:
        self._cfg = config
        self._env = dict(env) if env is not None else None
        self._command_override = list(command_override) if command_override else None
        self._resolved: str | None = None
        self._info = ProviderInfo(
            id=PROVIDER_ID,
            display_name="Claude Code CLI",
            local=False,
            roles=roles or [AiRole.CHAT, AiRole.REASON, AiRole.CODE, AiRole.BACKGROUND],
            status=HealthStatus.UNAVAILABLE,
            reason="not probed yet",
        )
        self._last: dict[str, AiResponse] = {}
        self.last_cost_usd: dict[str, float] = {}
        self.version: str = ""

    @property
    def info(self) -> ProviderInfo:
        return self._info

    @property
    def config(self) -> ClaudeCodeConfig:
        return self._cfg

    # -- command line ---------------------------------------------------------------------------

    def executable(self) -> list[str]:
        """Resolve the CLI once (``shutil.which`` handles .exe/.cmd on Windows)."""
        if self._command_override:
            return list(self._command_override)
        if self._resolved is None:
            found = shutil.which(self._cfg.command)
            if found is None:
                raise ProviderUnavailableError(
                    PROVIDER_ID, f"command not found: {self._cfg.command}"
                )
            self._resolved = found
        return [self._resolved]

    def build_args(
        self, system_prompt: str, *, model: str | None = None, max_budget_usd: float | None = None
    ) -> list[str]:
        """Arguments after the executable. The prompt itself goes to stdin (no length limit)."""
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--tools",
            "",
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
        ]
        if self._cfg.safe_mode:
            args.append("--safe-mode")
        use_model = model if model is not None else self._cfg.model
        if use_model:
            args += ["--model", use_model]
        budget = self._cfg.max_budget_usd if max_budget_usd is None else max_budget_usd
        if budget and budget > 0:
            args += ["--max-budget-usd", f"{budget:.4f}"]
        if system_prompt:
            args += ["--system-prompt", system_prompt]
        return args

    # -- health -------------------------------------------------------------------------------

    async def health(self) -> ProviderInfo:
        """``claude --version`` plus (if configured) a 1-token round-trip that verifies the login.
        The round-trip costs quota (about 0.005 USD equivalent on sonnet); the router caches it."""
        if not self._cfg.enabled:
            self._info = self._info.model_copy(
                update={"status": HealthStatus.UNAVAILABLE, "reason": "disabled in config"}
            )
            return self._info
        try:
            exe = self.executable()
        except ProviderUnavailableError as exc:
            self._info = self._info.model_copy(
                update={"status": HealthStatus.UNAVAILABLE, "reason": exc.message}
            )
            return self._info
        try:
            version = await self._run_simple([*exe, "--version"], timeout_s=15.0)
        except ProviderError as exc:
            self._info = self._info.model_copy(
                update={
                    "status": HealthStatus.UNAVAILABLE,
                    "reason": f"--version failed: {exc.message}",
                }
            )
            return self._info
        self.version = version.strip()
        if not self._cfg.health_roundtrip:
            self._info = self._info.model_copy(
                update={
                    "status": HealthStatus.LIMITED,
                    "reason": f"{self.version}; login not verified",
                }
            )
            return self._info
        probe = AiRequest(
            request_id="health-probe",
            role=AiRole.CHAT,
            messages=[Message(role="user", content="Reply with exactly: OK")],
            max_tokens=5,
            timeout_s=45.0,
        )
        try:
            response = await self.complete(probe, model=self._cfg.model or "haiku")
        except ProviderError as exc:
            self._info = self._info.model_copy(
                update={
                    "status": HealthStatus.UNAVAILABLE,
                    "reason": f"{self.version}; {exc.message}",
                }
            )
            return self._info
        self._info = self._info.model_copy(
            update={
                "status": HealthStatus.AVAILABLE,
                "reason": f"{self.version}; round-trip {response.latency_ms} ms",
            }
        )
        return self._info

    # -- AiProvider -----------------------------------------------------------------------------

    async def complete(self, request: AiRequest, *, model: str | None = None) -> AiResponse:
        parts: list[str] = []
        async for chunk in self._stream(request, model=model):
            if chunk.delta:
                parts.append(chunk.delta)
        result = self._last.pop(request.request_id, None)
        if result is None:  # cannot happen: _stream always records before the final chunk
            raise ProviderError(PROVIDER_ID, "stream ended without a result line")
        return result

    def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        return self._stream(request)

    def last_response(self, request_id: str) -> AiResponse | None:
        return self._last.pop(request_id, None)

    async def _stream(
        self, request: AiRequest, *, model: str | None = None
    ) -> AsyncIterator[AiChunk]:
        if not self._cfg.enabled:
            raise ProviderUnavailableError(PROVIDER_ID, "disabled in config")
        system_prompt, prompt = render_prompt(request)
        if not prompt.strip():
            raise ProviderError(PROVIDER_ID, "empty prompt", retryable=False)
        exe = self.executable()
        args = [*exe, *self.build_args(system_prompt, model=model or request.metadata.get("model"))]
        timeout_s = min(request.timeout_s, self._cfg.timeout_s)
        deadline = time.monotonic() + timeout_s
        started = time.perf_counter()
        parser = ClaudeStreamParser()
        parts: list[str] = []
        result: StreamItem | None = None
        first_token_ms: int | None = None

        proc = await self._spawn(args)
        assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None
        stderr_task = asyncio.ensure_future(proc.stderr.read(STDERR_CAPTURE_BYTES))
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderTimeoutError(PROVIDER_ID, f"timeout after {timeout_s:.0f}s")
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise ProviderTimeoutError(
                        PROVIDER_ID, f"timeout after {timeout_s:.0f}s"
                    ) from exc
                if not raw:
                    break
                item = parser.feed(raw.decode("utf-8", errors="replace"))
                if item is None:
                    continue
                if item.kind == "delta" and item.text:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    parts.append(item.text)
                    yield AiChunk(request_id=request.request_id, delta=item.text, done=False)
                elif item.kind == "result":
                    result = item
                    break
            await self._finish(proc, stderr_task, result)
        finally:
            await _kill(proc)
            if not stderr_task.done():
                stderr_task.cancel()
        assert result is not None
        text = "".join(parts) if parts else result.text
        response = AiResponse(
            request_id=request.request_id,
            provider=PROVIDER_ID,
            text=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
        )
        self._last[request.request_id] = response
        if result.cost_usd is not None:
            self.last_cost_usd[request.request_id] = result.cost_usd
        log.debug(
            "claude_code.completed",
            request_id=request.request_id,
            model=parser.model,
            first_token_ms=first_token_ms,
            latency_ms=response.latency_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
        )
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    async def _finish(
        self,
        proc: asyncio.subprocess.Process,
        stderr_task: asyncio.Future[bytes],
        result: StreamItem | None,
    ) -> None:
        """Wait for exit, then translate error results into exceptions."""
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except TimeoutError:
            await _kill(proc)
        stderr = ""
        try:
            stderr = (await asyncio.wait_for(stderr_task, timeout=2.0)).decode("utf-8", "replace")
        except (TimeoutError, asyncio.CancelledError):
            pass
        if result is None:
            detail = stderr.strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
            raise ProviderError(PROVIDER_ID, f"no result line ({detail[0][:200]})")
        if result.is_error:
            message = result.error or result.terminal_reason or "unknown error"
            lowered = message.lower()
            if "not logged in" in lowered or "login" in lowered or "authentication" in lowered:
                raise ProviderUnavailableError(PROVIDER_ID, message)
            raise ProviderError(PROVIDER_ID, message)

    async def _spawn(self, args: list[str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env if self._env is not None else None,
                limit=STDOUT_LINE_LIMIT,
                creationflags=_creationflags(),
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot start {args[0]}: {exc}") from exc
        except OSError as exc:
            raise ProviderError(PROVIDER_ID, f"cannot start {args[0]}: {exc}") from exc

    async def _run_simple(self, args: list[str], *, timeout_s: float) -> str:
        proc = await self._spawn(args)
        try:
            out, err = await asyncio.wait_for(proc.communicate(b""), timeout=timeout_s)
        except TimeoutError as exc:
            await _kill(proc)
            raise ProviderTimeoutError(PROVIDER_ID, f"{args[1:]} timed out") from exc
        finally:
            await _kill(proc)
        if proc.returncode != 0:
            raise ProviderError(
                PROVIDER_ID, f"exit {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}"
            )
        return out.decode("utf-8", "replace")


def _creationflags() -> int:
    # No console window for the child on Windows; the value is 0 elsewhere.
    if sys.platform == "win32":
        return int(getattr(os, "CREATE_NO_WINDOW", 0x08000000))
    return 0


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Terminate the child if still running (cancellation / kill switch path)."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        log.warning("claude_code.kill_timeout", pid=proc.pid)
