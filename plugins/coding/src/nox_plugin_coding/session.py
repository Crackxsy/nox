"""Story-coding session runner: a resumable, tool-enabled Claude Code CLI subprocess per session.

Deliberately separate from `nox.ai.providers.claude_code.ClaudeCodeProvider.build_args()`, which is
fixed to the one-shot REASON/CODE/BACKGROUND path (`--tools "" --max-turns 1
--no-session-persistence`, SP-01 Decision) - ST-14-02 calls the agentic, resumable case "a separate
wrapper". This module reuses only `ClaudeStreamParser` (text/result/status parsing) from
`claude_code.py`; it does not call or depend on `build_args()`.

Verified against Claude Code CLI 2.1.270 (`11 - Spikes/SP-18 Claude Code Session Wrapper.md`):
- `--tools <list>` (never `--allowedTools`, which does not narrow the usable tool set at all) is
  what keeps the session out of `Bash`/`PowerShell`/`WebFetch`/`Task`/... - no raw shell, no egress
  beyond the CLI's own login (spec §6.6).
- `--permission-mode acceptEdits` edits files without an interactive prompt while still being more
  restrictive than `bypassPermissions` - the most restrictive mode that still edits.
- `--resume <session_id>` continues the same session id and does not replay already-executed tool
  calls.
- A turn-limit stop is `result.subtype == "error_max_turns"`,
  `result.terminal_reason == "max_turns"` - distinct from a genuine failure; the repair loop below
  does not treat it as "fixable".
- `ClaudeStreamParser` never surfaces `tool_use` content blocks (it only tracks text), so
  `extract_tool_use` below does a small, separate pass over the same raw NDJSON line for progress
  reporting (tool name, file touched).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nox.ai.providers.claude_code import ClaudeStreamParser, StreamItem

#: Built-in Claude Code tools the session wrapper ever allows - no shell, no web egress, no
#: cross-session messaging. Nox's own tool layer mediates everything else (spec §6.6).
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep")
DEFAULT_PERMISSION_MODE = "acceptEdits"
DEFAULT_MAX_TURNS = 30
MAX_REPAIR_ATTEMPTS = 3
STDOUT_LINE_LIMIT = 4 * 1024 * 1024
STDERR_CAPTURE_BYTES = 16 * 1024

# Substrings of a failure reason that must never be treated as "fixable" by the repair loop.
_UNREPAIRABLE_MARKERS = ("logged in", "login", "authentication", "max turns", "max_turns")


class SessionStage(StrEnum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    TEST = "test"
    REPAIRING = "repairing"
    REVIEW = "review"
    MERGE = "merge"


class SessionOutcome(StrEnum):
    RUNNING = "running"
    ENDED = "ended"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SessionError(RuntimeError):
    def __init__(self, session_id: str, message: str) -> None:
        super().__init__(f"{session_id}: {message}")
        self.session_id = session_id
        self.message = message


class SessionNotFoundError(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, "unknown session")


class SessionUnavailableError(RuntimeError):
    """The `claude` command could not be resolved/spawned at all."""


@dataclass(slots=True)
class ToolUseEvent:
    """One `tool_use` content block pulled out of a raw NDJSON line (SP-18)."""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)

    @property
    def file_touched(self) -> str:
        for key in ("file_path", "path", "notebook_path"):
            value = self.tool_input.get(key)
            if isinstance(value, str) and value:
                return value
        return ""


@dataclass(slots=True)
class SessionRecord:
    """Nox-side bookkeeping for one story-coding session. Not persisted to SQLite (per the spec's
    "coding_sessions full transcripts are not stored" note); lives in memory plus whatever the vault
    session note records at close-out."""

    session_id: str
    workspace: str
    cli_session_id: str = ""
    stage: SessionStage = SessionStage.PLAN
    outcome: SessionOutcome = SessionOutcome.RUNNING
    repair_attempts: int = 0
    files_touched: list[str] = field(default_factory=list)
    last_error: str = ""
    last_result_text: str = ""
    process: asyncio.subprocess.Process | None = None

    def record_tool_use(self, event: ToolUseEvent) -> None:
        touched = event.file_touched
        if touched and touched not in self.files_touched:
            self.files_touched.append(touched)


ProgressHandler = Callable[
    [SessionRecord, SessionStage, ToolUseEvent | None], Awaitable[None] | None
]


def build_session_args(
    *,
    permission_mode: str,
    allowed_tools: Sequence[str],
    add_dir: str,
    max_turns: int,
    resume: str | None,
    model: str | None,
    system_prompt: str = "",
) -> list[str]:
    """Argument set for the agentic session wrapper - independent of
    `ClaudeCodeProvider.build_args()` (SP-18 Decision)."""
    args = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
        permission_mode,
        "--add-dir",
        add_dir,
        "--tools",
        ",".join(allowed_tools),
        "--max-turns",
        str(max_turns),
        "--strict-mcp-config",
    ]
    if model:
        args += ["--model", model]
    if system_prompt:
        args += ["--system-prompt", system_prompt]
    if resume:
        args += ["--resume", resume]
    return args


def extract_tool_use(raw_line: str) -> ToolUseEvent | None:
    """Pull a `tool_use` content block (name + input) out of one raw NDJSON line: the complete
    `assistant` message always carries the final `input` dict (SP-18); the partial
    `stream_event.content_block_start` only carries the tool name with an empty `input`, so it is
    used only as a fallback when no name has been seen yet for that block."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind == "assistant":
        message = data.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return ToolUseEvent(
                    tool_name=str(block.get("name", "")), tool_input=dict(block.get("input") or {})
                )
        return None
    if kind == "stream_event":
        event = data.get("event") or {}
        if event.get("type") == "content_block_start":
            block = event.get("content_block") or {}
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return ToolUseEvent(
                    tool_name=str(block.get("name", "")), tool_input=dict(block.get("input") or {})
                )
    return None


def _is_repairable(reason: str) -> bool:
    lowered = reason.lower()
    return not any(marker in lowered for marker in _UNREPAIRABLE_MARKERS)


class SessionRunner:
    """Owns every active Claude Code CLI subprocess for the `coding` plugin. One `SessionRunner`
    per plugin instance; sessions live only in memory (process + `SessionRecord`)."""

    def __init__(
        self,
        *,
        command: str = "claude",
        command_override: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        default_model: str | None = "sonnet",
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
        max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
        default_max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self._command = command
        self._command_override = list(command_override) if command_override else None
        self._env = dict(env) if env is not None else None
        self.default_model = default_model
        self.permission_mode = permission_mode
        self.allowed_tools = tuple(allowed_tools)
        self.max_repair_attempts = max_repair_attempts
        self.default_max_turns = default_max_turns
        self.sessions: dict[str, SessionRecord] = {}

    def executable(self) -> list[str]:
        if self._command_override:
            return list(self._command_override)
        found = shutil.which(self._command)
        if found is None:
            raise SessionUnavailableError(f"command not found: {self._command}")
        return [found]

    def get(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(
        self,
        *,
        workspace: str,
        prompt: str,
        system_prompt: str = "",
        max_turns: int | None = None,
        on_started: Callable[[SessionRecord], Awaitable[None] | None] | None = None,
        on_progress: ProgressHandler | None = None,
    ) -> SessionRecord:
        session_id = uuid.uuid4().hex
        record = SessionRecord(session_id=session_id, workspace=workspace)
        self.sessions[session_id] = record
        if on_started is not None:
            await _maybe_await(on_started(record))
        await self._run(
            record,
            prompt=prompt,
            system_prompt=system_prompt,
            max_turns=max_turns or self.default_max_turns,
            resume=None,
            on_progress=on_progress,
        )
        return record

    async def run_with_repair(
        self,
        *,
        workspace: str,
        prompt: str,
        system_prompt: str = "",
        max_turns: int | None = None,
        on_started: Callable[[SessionRecord], Awaitable[None] | None] | None = None,
        on_progress: ProgressHandler | None = None,
    ) -> SessionRecord:
        """Start a session; on a fixable-looking CLI failure, resume with a repair prompt up to
        `max_repair_attempts` times, then stop and report (Personality v1 B.8: no infinite retry,
        no silent stall)."""
        record = await self.start(
            workspace=workspace,
            prompt=prompt,
            system_prompt=system_prompt,
            max_turns=max_turns,
            on_started=on_started,
            on_progress=on_progress,
        )
        while (
            record.outcome is SessionOutcome.FAILED
            and _is_repairable(record.last_error)
            and record.repair_attempts < self.max_repair_attempts
        ):
            record.repair_attempts += 1
            record.stage = SessionStage.REPAIRING
            if on_progress is not None:
                await _maybe_await(on_progress(record, SessionStage.REPAIRING, None))
            repair_prompt = (
                f"The previous attempt failed: {record.last_error}\n"
                "Diagnose the cause and fix it. Make the smallest change that resolves it."
            )
            await self._run(
                record,
                prompt=repair_prompt,
                system_prompt="",
                max_turns=max_turns or self.default_max_turns,
                resume=record.cli_session_id or None,
                on_progress=on_progress,
            )
        return record

    async def resume(
        self,
        session_id: str,
        *,
        prompt: str,
        max_turns: int | None = None,
        on_progress: ProgressHandler | None = None,
    ) -> SessionRecord:
        record = self.sessions.get(session_id)
        if record is None:
            raise SessionNotFoundError(session_id)
        await self._run(
            record,
            prompt=prompt,
            system_prompt="",
            max_turns=max_turns or self.default_max_turns,
            resume=record.cli_session_id or None,
            on_progress=on_progress,
        )
        return record

    async def cancel(self, session_id: str, *, reason: str = "cancelled") -> SessionRecord | None:
        record = self.sessions.get(session_id)
        if record is None:
            return None
        # Decide the outcome *before* killing the process: `_run`'s own loop may notice the same
        # EOF concurrently and call `_finish`, which must not overwrite a cancellation with a
        # generic "no result line" failure - `_finish` only ever sets an outcome while it is still
        # RUNNING, so setting CANCELLED first always wins the race.
        if record.outcome is SessionOutcome.RUNNING:
            record.outcome = SessionOutcome.CANCELLED
            record.last_error = reason
        if record.process is not None:
            await _kill(record.process)
        return record

    async def terminate_all(self, *, reason: str = "kill_switch") -> list[SessionRecord]:
        """`security.kill_switch`: kill every active subprocess, never leave one running
        unattended (Personality v1 B.8)."""
        terminated: list[SessionRecord] = []
        for record in self.sessions.values():
            if record.outcome is SessionOutcome.RUNNING:
                record.outcome = SessionOutcome.CANCELLED
                record.last_error = reason
                if record.process is not None:
                    await _kill(record.process)
                terminated.append(record)
        return terminated

    # -- subprocess -------------------------------------------------------------------------------

    async def _run(
        self,
        record: SessionRecord,
        *,
        prompt: str,
        system_prompt: str,
        max_turns: int,
        resume: str | None,
        on_progress: ProgressHandler | None,
    ) -> None:
        args = [
            *self.executable(),
            *build_session_args(
                permission_mode=self.permission_mode,
                allowed_tools=self.allowed_tools,
                add_dir=record.workspace,
                max_turns=max_turns,
                resume=resume,
                model=self.default_model,
                system_prompt=system_prompt,
            ),
        ]
        record.stage = SessionStage.IMPLEMENT
        proc = await self._spawn(args, cwd=record.workspace)
        record.process = proc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.ensure_future(proc.stderr.read(STDERR_CAPTURE_BYTES))
        parser = ClaudeStreamParser()
        result: StreamItem | None = None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                tool_event = extract_tool_use(text)
                if tool_event is not None and tool_event.tool_name:
                    record.record_tool_use(tool_event)
                    if on_progress is not None:
                        await _maybe_await(on_progress(record, record.stage, tool_event))
                item = parser.feed(text)
                if item is None:
                    continue
                if item.session_id:
                    record.cli_session_id = item.session_id
                if item.kind == "result":
                    result = item
                    break
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except TimeoutError:
            pass
        finally:
            await _kill(proc)
            if not stderr_task.done():
                stderr_task.cancel()
        self._finish(record, result)

    def _finish(self, record: SessionRecord, result: StreamItem | None) -> None:
        record.process = None
        if record.outcome is not SessionOutcome.RUNNING:
            # Already decided externally (cancel()/terminate_all() beat us to it) - never
            # overwrite a cancellation with a generic "no result line" failure.
            return
        if result is None:
            record.outcome = SessionOutcome.FAILED
            record.last_error = "no result line (process ended without one)"
            return
        record.last_result_text = result.text
        if result.is_error:
            reason = result.error or result.terminal_reason or "unknown error"
            if result.terminal_reason == "max_turns":
                reason = f"max turns reached before finishing: {reason}"
            record.last_error = reason
            record.outcome = SessionOutcome.FAILED
        else:
            record.outcome = SessionOutcome.ENDED
            record.last_error = ""
            record.stage = SessionStage.REVIEW

    async def _spawn(self, args: list[str], *, cwd: str) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._env if self._env is not None else None,
                limit=STDOUT_LINE_LIMIT,
                creationflags=_creationflags(),
            )
        except FileNotFoundError as exc:
            raise SessionUnavailableError(f"cannot start {args[0]}: {exc}") from exc
        except OSError as exc:
            raise SessionError("", f"cannot start {args[0]}: {exc}") from exc


async def _maybe_await(result: Any) -> None:
    if hasattr(result, "__await__"):
        await result


def _creationflags() -> int:
    if sys.platform == "win32":
        return int(getattr(os, "CREATE_NO_WINDOW", 0x08000000))
    return 0


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        pass
