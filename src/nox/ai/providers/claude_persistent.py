"""The conversation path of the Claude Code provider: one warm session, Nox's tools over MCP.

`ClaudeCodeProvider` sends a request here when it belongs to the assistant's conversation (the
orchestrator marks those with a `session_id`). Everything else - the stream chat responder,
memory capture, the health probe - keeps the one-shot `claude -p`, so a different persona never
lands in, or restarts, the assistant's session.

Arguments, and why (verified in `spikes/sp13_native_tools.py` against Claude Code 2.1.3xx):

* `--tools ""` keeps Claude Code's own Read/Bash/WebFetch off - they would run outside Nox's
  permission engine - and does **not** disable MCP tools.
* `--mcp-config <file> --strict-mcp-config --allowedTools mcp__nox__*` gives it exactly Nox's tools
  through the bridge; the pre-approval only stops the CLI from asking itself, Nox decides.
* `--safe-mode` is **not** used here: it also drops an explicit `--mcp-config` (measured). The
  same isolation comes from `--setting-sources ""` (no user/project settings, hooks or plugins),
  `--disable-slash-commands` (no skills) and a working directory of its own (no CLAUDE.md).
* `WebSearch` (runs at Anthropic, returns text) is allowed only in privacy mode full/balanced.
* `MCP_TOOL_TIMEOUT` is set explicitly above the confirmation window plus a tool's own timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from pathlib import Path
from typing import Protocol

from nox.ai._log import get_logger
from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, Message
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.ai.providers.claude_session import ClaudeSession, SessionSpec
from nox.ai.providers.claude_stream import ClaudeStreamParser, StreamItem
from nox.tools.names import from_wire

log = get_logger(__name__)

PROVIDER_ID = "claude_code"
MCP_SERVER = "nox"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER}__"
#: Confirmation window (60 s) + the longest tool timeout Nox grants by default + margin, in ms.
MCP_TOOL_TIMEOUT_MS = 200_000
#: Privacy modes in which the model may use Anthropic's server-side web search.
WEB_SEARCH_MODES = frozenset({"full", "balanced"})
#: Extra time a turn with tools may take beyond the turn budget (dialogs, the final answer).
TOOL_TURN_MARGIN_S = 30.0

#: Appended to the system prompt of a session: the per-turn context arrives inside the user message.
SESSION_NOTE = """\
Each user message may start with a <turn_context> block that Nox adds (current time, facts about \
the user, retrieved notes, what is unavailable right now) and an <earlier_conversation> block with \
turns you have not seen. Both are background written by Nox, not words of the user; the user's \
message follows them."""


class ToolBridge(Protocol):
    """The core side of the MCP bridge (`nox.mcp.gate.McpGate`), as far as the provider needs it."""

    def prepare(self) -> Launch: ...
    async def activate(self, launch: Launch, token: str) -> bool: ...
    async def retire(self, launch: Launch) -> None: ...


class Launch(Protocol):
    @property
    def client_id(self) -> str: ...
    @property
    def config_path(self) -> Path: ...


def _key(message: Message) -> str:
    return hashlib.sha256(f"{message.role}\0{message.content}".encode()).hexdigest()


def display_tool(name: str) -> str:
    """`mcp__nox__file-read` -> `file.read`; other tools (WebSearch) keep their name."""
    if name.startswith(MCP_TOOL_PREFIX):
        return from_wire(name[len(MCP_TOOL_PREFIX) :])
    return name


def clean_env(base: Mapping[str, str]) -> dict[str, str]:
    """The CLI's environment: no variables of an outer Claude Code session (a developer running
    Nox from one), and the MCP tool timeout set explicitly."""
    env = {
        key: value
        for key, value in base.items()
        if not key.startswith(("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CLAUDE_JOB"))
    }
    env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    return env


class PersistentClaude:
    """Owns the conversation session of one core: at most one Claude Code process at a time."""

    def __init__(
        self,
        config: ClaudeCodeConfig,
        *,
        executable: Callable[[], list[str]],
        creationflags: int,
        workdir: Path | None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._cfg = config
        self._executable = executable
        self._creationflags = creationflags
        self._workdir = workdir
        self._env = clean_env(env if env is not None else os.environ)
        self._bridge: ToolBridge | None = None
        self._session: ClaudeSession | None = None
        self._launch: Launch | None = None
        #: The Nox conversation the session belongs to; `None` = started ahead of time, unbound.
        self._conversation: str | None = None
        self._dirty = False
        self._killed = False
        #: Bumped by every kill; a start that began in an older epoch is discarded.
        self._epoch = 0
        self._lifecycle = asyncio.Lock()
        self._starting: tuple[ClaudeSession, Launch | None] | None = None
        self.last: dict[str, AiResponse] = {}

    def attach_tools(self, bridge: ToolBridge) -> None:
        self._bridge = bridge

    @property
    def pid(self) -> int | None:
        return self._session.pid if self._session is not None and self._session.alive else None

    # -- arguments ---------------------------------------------------------------------------------

    def spec(self, system_prompt: str, privacy_mode: str, mcp_config: Path | None) -> SessionSpec:
        web = self._cfg.web_search and privacy_mode in WEB_SEARCH_MODES
        args = [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--no-session-persistence",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--tools",
            "WebSearch" if web else "",
            "--max-turns",
            str(self._cfg.session_max_turns),
        ]
        allowed = [f"{MCP_TOOL_PREFIX}*"] if mcp_config is not None else []
        if web:
            allowed.append("WebSearch")
        if mcp_config is not None:
            args += ["--mcp-config", str(mcp_config)]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if self._cfg.model:
            args += ["--model", self._cfg.model]
        if self._cfg.effort:
            args += ["--effort", self._cfg.effort]
        prompt = f"{system_prompt}\n\n{SESSION_NOTE}" if system_prompt else SESSION_NOTE
        args += ["--system-prompt", prompt]
        return SessionSpec(
            args=tuple(args), env=self._env, cwd=self._workdir, model=self._cfg.model or "default"
        )

    # -- lifecycle ---------------------------------------------------------------------------------

    async def prewarm(self, system_prompt: str, privacy_mode: str) -> None:
        """Start a session before the first turn, so the user never waits for a cold start."""
        if self._killed:
            return
        await self._ensure(system_prompt, privacy_mode, conversation=None)

    async def _ensure(
        self, system_prompt: str, privacy_mode: str, *, conversation: str | None
    ) -> ClaudeSession:
        # One start at a time. The kill switch does not take this lock - it must never wait for a
        # slow start - it kills a starting session directly (`_starting`) and bumps the epoch, so
        # a start that was overtaken by a kill can never store its session.
        async with self._lifecycle:
            return await self._ensure_locked(system_prompt, privacy_mode, conversation)

    async def _ensure_locked(
        self, system_prompt: str, privacy_mode: str, conversation: str | None
    ) -> ClaudeSession:
        if self._killed:
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        # The config path differs per spawn and is not part of the fingerprint, but whether there
        # is one at all is: compare like with like.
        placeholder = Path("mcp.json") if self._bridge is not None else None
        wanted = self.spec(system_prompt, privacy_mode, placeholder).fingerprint
        session = self._session
        reusable = (
            session is not None
            and session.alive
            and session.spec.fingerprint == wanted
            and session.turns < self._cfg.session_turns
            and (self._conversation is None or conversation in (None, self._conversation))
        )
        if reusable:
            assert session is not None
            if conversation is not None:
                self._conversation = conversation
            return session
        await self.close()
        epoch = self._epoch
        launch = self._bridge.prepare() if self._bridge is not None else None
        spec = self.spec(system_prompt, privacy_mode, launch.config_path if launch else None)
        if self._workdir is not None:
            self._workdir.mkdir(parents=True, exist_ok=True)
        session = ClaudeSession(spec, creationflags=self._creationflags)
        self._starting = (session, launch)
        try:
            await session.start(self._executable())
            await session.initialize()
        except ProviderError:
            await self._discard(session, launch)
            if self._killed or epoch != self._epoch:
                raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch") from None
            raise
        except BaseException:
            await self._discard(session, launch)
            raise
        finally:
            self._starting = None
        if self._killed or epoch != self._epoch:
            await self._discard(session, launch)
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        self._session, self._launch, self._conversation = session, launch, conversation
        self._dirty = False
        return session

    async def _discard(self, session: ClaudeSession, launch: Launch | None) -> None:
        session.kill()
        if launch is not None and self._bridge is not None:
            await self._bridge.retire(launch)

    async def close(self) -> None:
        session, launch = self._session, self._launch
        self._session, self._launch = None, None
        if session is not None:
            await session.close()
        if launch is not None and self._bridge is not None:
            await self._bridge.retire(launch)

    async def kill(self) -> None:
        """Kill switch: every process tree dies now - the running session and one still
        starting - the bridges are cut off, and nothing starts again until `resume`."""
        self._killed = True
        self._epoch += 1
        doomed = [(self._session, self._launch)]
        if self._starting is not None:
            doomed.append(self._starting)
        self._session, self._launch = None, None
        for session, _launch in doomed:
            if session is not None:
                session.kill()
        for _session, launch in doomed:
            if launch is not None and self._bridge is not None:
                await self._bridge.retire(launch)

    def resume(self) -> None:
        self._killed = False

    # -- one turn ----------------------------------------------------------------------------------

    def _model_for(self, request: AiRequest) -> str:
        override = request.metadata.get("model", "")
        if override:
            return override
        if request.role is AiRole.REASON and self._cfg.reason_model:
            return self._cfg.reason_model
        return self._cfg.model or "default"

    def _compose(self, session: ClaudeSession, request: AiRequest, turns: list[Message]) -> str:
        """The user message for this turn: context, what the session missed, the user's words."""
        history, current = turns[:-1], turns[-1]
        missing = [m for m in history if _key(m) not in session.seen]
        parts = []
        if request.context:
            parts.append(f"<turn_context>\n{request.context}\n</turn_context>")
        if missing:
            lines = [f"{'User' if m.role == 'user' else 'Nox'}: {m.content}" for m in missing]
            parts.append(
                "<earlier_conversation>\n" + "\n".join(lines) + "\n</earlier_conversation>"
            )
        parts.append(current.content)
        session.seen.update(_key(m) for m in turns)
        return "\n\n".join(parts)

    def _timeout(self, request: AiRequest) -> float:
        if request.tools:
            return max(request.timeout_s, self._cfg.timeout_s)
        return min(request.timeout_s, self._cfg.timeout_s)

    async def stream(self, request: AiRequest) -> AsyncGenerator[AiChunk]:
        if self._killed:
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [m for m in request.messages if m.role in ("user", "assistant")]
        if not turns or turns[-1].role != "user" or not turns[-1].content.strip():
            raise ProviderError(PROVIDER_ID, "empty prompt", retryable=False)
        conversation = request.metadata.get("session_id", "")
        session = await self._ensure(system, request.privacy_mode, conversation=conversation)
        async with session.lock:
            if self._dirty and not await session.drain_until_result(2.0):
                session.kill()
                session = await self._ensure(system, request.privacy_mode, conversation=None)
            self._dirty = False
            # `aclosing`: when the consumer stops early (barge-in), the inner generator must be
            # closed now - its `finally` sends the interrupt - not whenever it is collected.
            async with contextlib.aclosing(self._turn(session, request, turns)) as chunks:
                async for chunk in chunks:
                    yield chunk

    async def _turn(
        self, session: ClaudeSession, request: AiRequest, turns: list[Message]
    ) -> AsyncGenerator[AiChunk]:
        started = time.perf_counter()
        deadline = time.monotonic() + self._timeout(request)
        if request.tools and request.tool_turn and self._bridge and self._launch:
            if not await self._bridge.activate(self._launch, request.tool_turn):
                # Normal for a session whose bridge is still connecting: the gate delivers the
                # token the moment it is in.
                log.debug("claude_persistent.token_pending")
        await session.set_model(self._model_for(request))
        await session.send(self._compose(session, request, turns))
        parser = ClaudeStreamParser()
        parts: list[str] = []
        first_token_ms: int | None = None
        finished = False
        #: Text after a tool call is a new block; without a space it would run into the last one.
        after_tool = False
        try:
            while True:
                line = await session.next_line(deadline)
                if line is None:
                    raise ProviderError(PROVIDER_ID, "the Claude Code session ended mid-turn")
                item = parser.feed(line)
                if item is None:
                    continue
                if item.kind == "delta" and item.text:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    text = item.text
                    if after_tool and parts and not parts[-1][-1:].isspace():
                        text = " " + text.lstrip()
                    after_tool = False
                    parts.append(text)
                    yield AiChunk(request_id=request.request_id, delta=text)
                elif item.kind == "tool" and item.text:
                    after_tool = True
                    yield AiChunk(
                        request_id=request.request_id,
                        delta="",
                        tool_activity=[display_tool(item.text)],
                    )
                elif item.kind == "result":
                    finished = True
                    self._finish(session, request, item, parts, started, first_token_ms)
                    break
        except ProviderTimeoutError:
            self._abandon(session)
            raise
        except ProviderError:
            session.kill()
            raise
        finally:
            if not finished and session.alive and not self._killed:
                # Barge-in or cancellation: stop the answer now; the next turn drains what is
                # left (or replaces the process if it does not wind down).
                self._abandon(session)
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    def _abandon(self, session: ClaudeSession) -> None:
        self._dirty = True
        with contextlib.suppress(Exception):
            session.request_interrupt()

    def _finish(
        self,
        session: ClaudeSession,
        request: AiRequest,
        item: StreamItem,
        parts: list[str],
        started: float,
        first_token_ms: int | None,
    ) -> None:
        text = "".join(parts) or item.text
        if item.is_error and not parts:
            message = item.error or item.terminal_reason or "unknown error"
            lowered = message.lower()
            if "login" in lowered or "authentication" in lowered:
                raise ProviderUnavailableError(PROVIDER_ID, message)
            raise ProviderError(PROVIDER_ID, message)
        session.seen.add(_key(Message(role="assistant", content=text)))
        cost = session.cost_delta(item.cost_usd)
        response = AiResponse(
            request_id=request.request_id,
            provider=PROVIDER_ID,
            text=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=item.tokens_in,
            tokens_out=item.tokens_out,
            cost_usd=cost,
        )
        self.last[request.request_id] = response
        log.debug(
            "claude_persistent.turn",
            request_id=request.request_id,
            model=session.model,
            first_token_ms=first_token_ms,
            latency_ms=response.latency_ms,
            cost_usd=cost,
        )
