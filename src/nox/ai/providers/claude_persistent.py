"""The conversation path of the Claude Code provider: one warm session, Nox's tools over MCP.

`ClaudeCodeProvider` sends a request here when it belongs to the assistant's conversation (the
orchestrator marks those with a `session_id`). Everything else - the stream chat responder,
memory capture, the health probe - keeps the one-shot `claude -p`, so a different persona never
lands in, or restarts, the assistant's session.

Arguments, and why (verified against Claude Code 2.1.3xx, `spikes/results/sp13_results.json` and
`tests/integration/test_claude_isolation.py`):

* `--tools ""` keeps Claude Code's own Read/Bash/WebFetch off - they would run outside Nox's
  permission engine - and does **not** disable MCP tools.
* `--mcp-config <file> --strict-mcp-config --allowedTools mcp__nox__*` gives it exactly Nox's tools
  through the bridge; the pre-approval only stops the CLI from asking itself, Nox decides.
* `--safe-mode` is **not** used: it also drops an explicit `--mcp-config` (measured). Isolation
  instead: `--setting-sources ""` (no user/project/local settings, hooks, plugins, CLAUDE.md),
  `--disable-slash-commands` (no skills), auto-memory off (`--settings` and
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY` - without both, a project's auto-memory reached the prompt),
  a working folder of its own in the system temp folder, and a home folder of its own
  (`HOME`/`USERPROFILE`) - only `CLAUDE_CONFIG_DIR` points at the real configuration, so the login
  keeps working.
* The child gets an allow-listed environment (`child_env`): nothing of an outer Claude Code
  session and no Nox token or secret that happens to be in Nox's own environment.
* `WebSearch` (runs at Anthropic, returns text) only in privacy mode full/balanced, at most
  `MAX_WEB_SEARCHES_PER_TURN` per turn.
* `--max-turns` is **not** enforced in stream-json mode (measured: three tool calls under
  `--max-turns 2`); the limit is Nox's `TurnBudget`. `--max-budget-usd` is cumulative per session
  (measured), so it is passed as a session cap and the session is replaced when it is reached.
* `MCP_TOOL_TIMEOUT` is set explicitly above the confirmation window plus a tool's own timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from nox.ai._log import get_logger
from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, Message
from nox.ai.config import ClaudeCodeConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.ai.providers.claude_cost import TurnCost
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
WEB_SEARCH_TOOL = "WebSearch"
#: Web searches one turn may make before Nox interrupts it; they run at Anthropic, outside the
#: tool loop, so this is their budget.
MAX_WEB_SEARCHES_PER_TURN = 5
#: Extra time a turn with tools may take beyond the turn budget (dialogs, the final answer).
TOOL_TURN_MARGIN_S = 30.0
#: Failed starts or control requests after which the warm session is given up for this run.
MAX_SESSION_FAILURES = 3
DRAIN_GRACE_S = 2.0
#: Said when a turn is stopped because its estimated cost passed `max_budget_usd`.
COST_LIMIT_NOTICE = {
    "de": " Ich höre hier auf: Das Kostenlimit für eine Antwort ist erreicht.",
    "en": " I am stopping here: the cost limit for one answer is reached.",
}

#: Environment variables the CLI process may inherit. Everything else - an outer Claude Code
#: session's variables, Nox's own tokens, any secret that happens to be set - stays behind.
INHERITED_ENV = frozenset(
    {
        # The operating system and the runtime.
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "TMPDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "LANG",
        "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL", "XDG_RUNTIME_DIR",
        # Networks that need a proxy or a corporate CA.
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        # How the CLI logs in and which backend it uses - set by the user for Claude Code.
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
        "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS",
    }
)  # fmt: skip

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


def real_config_dir(base: Mapping[str, str]) -> Path:
    """Where the user's Claude Code login lives, resolved from Nox's own environment."""
    explicit = base.get("CLAUDE_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    home = base.get("USERPROFILE") or base.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def child_env(
    base: Mapping[str, str],
    *,
    home: Path | None = None,
    also_inherit: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """The CLI's environment: the allow-list above (plus `also_inherit`, for tests), a home folder
    of its own, the real config folder (for the login) and the switches Nox relies on."""
    allowed = INHERITED_ENV | also_inherit
    env = {key: value for key, value in base.items() if key.upper() in allowed}
    env["CLAUDE_CONFIG_DIR"] = str(real_config_dir(base))
    if home is not None:
        env["HOME"] = env["USERPROFILE"] = str(home)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["MCP_TOOL_TIMEOUT"] = str(MCP_TOOL_TIMEOUT_MS)
    return env


#: The name earlier releases used; same function.
clean_env = child_env


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
        also_inherit: frozenset[str] = frozenset(),
    ) -> None:
        self._cfg = config
        self._also_inherit = also_inherit
        self._executable = executable
        self._creationflags = creationflags
        #: Created on first use under the system temp folder when not given: outside the Nox
        #: data folder and outside any repository, so no CLAUDE.md is above it.
        self._root = workdir
        self._base_env = dict(env if env is not None else os.environ)
        self._bridge: ToolBridge | None = None
        self._session: ClaudeSession | None = None
        self._launch: Launch | None = None
        #: The Nox conversation the session belongs to; `None` = started ahead of time, unbound.
        self._conversation: str | None = None
        self._dirty = False
        self._killed = False
        #: The session must be replaced before the next turn (bridge lost, session budget spent).
        self._stale = False
        #: Bumped by every kill; a start that began in an older epoch is discarded.
        self._epoch = 0
        self._lifecycle = asyncio.Lock()
        self._starting: tuple[ClaudeSession, Launch | None] | None = None
        self.failures = 0
        #: What went wrong last, with the end of the CLI's stderr - for the log and the card.
        self.last_failure = ""
        #: Set when the warm session was given up; the provider then answers one-shot.
        self.disabled_reason = ""
        self.last: dict[str, AiResponse] = {}

    def attach_tools(self, bridge: ToolBridge) -> None:
        self._bridge = bridge

    @property
    def pid(self) -> int | None:
        return self._session.pid if self._session is not None and self._session.alive else None

    @property
    def disabled(self) -> bool:
        return bool(self.disabled_reason)

    def _folders(self) -> tuple[Path, Path]:
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="nox-claude-"))
        work, home = self._root / "work", self._root / "home"
        for folder in (work, home):
            folder.mkdir(parents=True, exist_ok=True)
        return work, home

    # -- arguments ---------------------------------------------------------------------------------

    def spec(
        self,
        system_prompt: str,
        privacy_mode: str,
        mcp_config: Path | None,
        offer: Sequence[str] = (),
    ) -> SessionSpec:
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
            "--settings",
            json.dumps({"autoMemoryEnabled": False}),
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--tools",
            WEB_SEARCH_TOOL if web else "",
        ]
        allowed = [f"{MCP_TOOL_PREFIX}*"] if mcp_config is not None else []
        if web:
            allowed.append(WEB_SEARCH_TOOL)
        if mcp_config is not None:
            args += ["--mcp-config", str(mcp_config)]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if self._cfg.model:
            args += ["--model", self._cfg.model]
        if self._cfg.effort:
            args += ["--effort", self._cfg.effort]
        if self._cfg.max_budget_usd > 0:
            # Cumulative per session in this mode: the cap of a whole session, which is replaced
            # when it is reached.
            cap = self._cfg.max_budget_usd * self._cfg.session_turns
            args += ["--max-budget-usd", f"{cap:.4f}"]
        prompt = f"{system_prompt}\n\n{SESSION_NOTE}" if system_prompt else SESSION_NOTE
        args += ["--system-prompt", prompt]
        work, home = self._folders()
        return SessionSpec(
            args=tuple(args),
            env=child_env(self._base_env, home=home, also_inherit=self._also_inherit),
            cwd=work,
            model=self._cfg.model or "default",
            offer=tuple(sorted(offer)),
        )

    # -- lifecycle ---------------------------------------------------------------------------------

    async def prewarm(
        self, system_prompt: str, privacy_mode: str, offer: Sequence[str] = ()
    ) -> None:
        """Start a session before the first turn, so the user never waits for a cold start.
        `offer` should be what the first turn will offer; a different one starts afresh."""
        if self._killed or self.disabled:
            return
        await self._ensure(system_prompt, privacy_mode, conversation=None, offer=offer)

    async def _ensure(
        self,
        system_prompt: str,
        privacy_mode: str,
        *,
        conversation: str | None,
        offer: Sequence[str] = (),
    ) -> ClaudeSession:
        # One start at a time. The kill switch does not take this lock - it must never wait for a
        # slow start - it kills a starting session directly (`_starting`) and bumps the epoch, so
        # a start that was overtaken by a kill can never store its session.
        async with self._lifecycle:
            return await self._ensure_locked(system_prompt, privacy_mode, conversation, offer)

    async def _ensure_locked(
        self,
        system_prompt: str,
        privacy_mode: str,
        conversation: str | None,
        offer: Sequence[str],
    ) -> ClaudeSession:
        if self._killed:
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        # The config path differs per spawn and is not part of the fingerprint, but whether there
        # is one at all is: compare like with like. The offer is part of it: Claude Code lists
        # the tools once, at start, so a different offer needs a different process.
        placeholder = Path("mcp.json") if self._bridge is not None else None
        wanted = self.spec(system_prompt, privacy_mode, placeholder, offer).fingerprint
        session = self._session
        reusable = (
            session is not None
            and session.alive
            and not self._stale
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
        if self._killed:  # the kill switch fired while the old session was closing
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        epoch = self._epoch
        launch = self._bridge.prepare() if self._bridge is not None else None
        spec = self.spec(system_prompt, privacy_mode, launch.config_path if launch else None, offer)
        session = ClaudeSession(spec, creationflags=self._creationflags)
        self._starting = (session, launch)
        try:
            await session.start(self._executable())
            await session.initialize()
        except ProviderError as exc:
            tail = session.stderr_tail()
            await self._discard(session, launch)
            if self._killed or epoch != self._epoch:
                raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch") from None
            self._failed("start", exc, tail)
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
        self._dirty = self._stale = False
        return session

    def _failed(self, stage: str, exc: Exception, stderr_tail: str) -> None:
        """Count a failure of the session machinery; after a few, give the warm session up."""
        self.failures += 1
        self.last_failure = f"{stage}: {str(exc)[:200]} | stderr: {stderr_tail[-400:]}"
        log.warning(
            "claude_persistent.failed",
            stage=stage,
            error=str(exc)[:300],
            stderr=stderr_tail[-800:],
            failures=self.failures,
        )
        if self.failures >= MAX_SESSION_FAILURES and not self.disabled:
            self.disabled_reason = f"{self.failures} failures, last in {stage}: {str(exc)[:200]}"
            log.error("claude_persistent.disabled", reason=self.disabled_reason)

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

    def halt(self) -> None:
        """Stop the answer in progress now (privacy changed under it). The session survives;
        the next turn drains what is left of this one."""
        session = self._session
        if session is not None and session.alive and session.lock.locked():
            self._abandon(session)

    def bridge_lost(self, client_id: str) -> None:
        """The bridge of the running session dropped out: without it every tool call fails, so
        the next turn starts a fresh session (and a fresh bridge)."""
        if self._launch is not None and self._launch.client_id == client_id:
            log.warning("claude_persistent.bridge_lost", client=client_id)
            self._stale = True

    # -- one turn ----------------------------------------------------------------------------------

    def _model_for(self, request: AiRequest) -> str:
        override = request.metadata.get("model", "")
        if override:
            return override
        if request.role is AiRole.REASON and self._cfg.reason_model:
            return self._cfg.reason_model
        return self._cfg.model or "default"

    def _compose(self, session: ClaudeSession, request: AiRequest, turns: list[Message]) -> str:
        """The user message for this turn: context, what the session missed, the user's words.

        History marked `private` (said while cloud models were not allowed) is never sent.
        """
        history, current = turns[:-1], turns[-1]
        missing = [m for m in history if not m.private and _key(m) not in session.seen]
        parts = []
        if request.context:
            parts.append(f"<turn_context>\n{request.context}\n</turn_context>")
        if missing:
            lines = [f"{'User' if m.role == 'user' else 'Nox'}: {m.content}" for m in missing]
            parts.append(
                "<earlier_conversation>\n" + "\n".join(lines) + "\n</earlier_conversation>"
            )
        parts.append(current.content)
        session.seen.update(_key(m) for m in turns if not m.private)
        return "\n\n".join(parts)

    def _timeout(self, request: AiRequest) -> float:
        if request.tools:
            return max(request.timeout_s, self._cfg.timeout_s)
        return min(request.timeout_s, self._cfg.timeout_s)

    async def stream(self, request: AiRequest) -> AsyncGenerator[AiChunk]:
        if self._killed:
            raise ProviderUnavailableError(PROVIDER_ID, "stopped by the kill switch")
        if self.disabled:
            raise ProviderUnavailableError(PROVIDER_ID, f"session off: {self.disabled_reason}")
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        turns = [m for m in request.messages if m.role in ("user", "assistant")]
        if not turns or turns[-1].role != "user" or not turns[-1].content.strip():
            raise ProviderError(PROVIDER_ID, "empty prompt", retryable=False)
        conversation = request.metadata.get("session_id", "")
        offer = [tool.name for tool in request.tools]
        session = await self._ensure(
            system, request.privacy_mode, conversation=conversation, offer=offer
        )
        async with session.lock:
            if self._dirty and not await session.drain_until_result(DRAIN_GRACE_S):
                # The interrupted answer did not wind down: replace the process. The new one has
                # seen nothing, so this turn carries the history it needs.
                session.kill()
                session = await self._ensure(
                    system, request.privacy_mode, conversation=conversation, offer=offer
                )
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
        parser = ClaudeStreamParser()
        parts: list[str] = []
        first_token_ms: int | None = None
        finished = False
        #: Text after a tool call is a new block; without a space it would run into the last one.
        after_tool = False
        searches = 0
        cost = TurnCost(model=session.model)
        over_budget = False
        try:
            await session.set_model(self._model_for(request))
            await session.send(self._compose(session, request, turns))
            while True:
                line = await session.next_line(deadline)
                if line is None:
                    raise ProviderError(PROVIDER_ID, "the Claude Code session ended mid-turn")
                item = parser.feed(line)
                if item is None:
                    continue
                if item.kind == "status" and item.text == "message_start":
                    cost.step_started(
                        input_tokens=item.tokens_in or 0,
                        cache_write_tokens=item.cache_write_tokens or 0,
                        cache_read_tokens=item.cache_read_tokens or 0,
                    )
                elif item.kind == "status" and item.text == "message_delta" and item.tokens_out:
                    cost.output_so_far(item.tokens_out)
                if not over_budget and item.kind != "result" and self._over_budget(cost):
                    over_budget = True
                    log.warning("claude_persistent.turn_cost_limit", estimate_usd=cost.usd)
                    session.request_interrupt()
                    notice = COST_LIMIT_NOTICE.get(
                        request.metadata.get("language", "de")[:2], COST_LIMIT_NOTICE["en"]
                    )
                    parts.append(notice)
                    yield AiChunk(request_id=request.request_id, delta=notice)
                    continue
                if over_budget and item.kind != "result":
                    continue  # nothing more from a turn that was stopped for its cost
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
                    if item.text == WEB_SEARCH_TOOL:
                        searches += 1
                        if searches > MAX_WEB_SEARCHES_PER_TURN:
                            log.warning("claude_persistent.web_search_budget", searches=searches)
                            session.request_interrupt()
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
        except ProviderError as exc:
            tail = session.stderr_tail()
            session.kill()
            if not self._killed:
                self._failed("turn", exc, tail)
            raise
        finally:
            if not finished and session.alive and not self._killed:
                # Barge-in or cancellation: stop the answer now; the next turn drains what is
                # left (or replaces the process if it does not wind down).
                self._abandon(session)
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    def _over_budget(self, cost: TurnCost) -> bool:
        return self._cfg.max_budget_usd > 0 and cost.usd > self._cfg.max_budget_usd

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
        if item.is_error and "budget" in (item.terminal_reason + item.error + item.text).lower():
            # The session's cost cap is reached; every further message would fail the same way.
            log.warning("claude_persistent.session_budget_reached")
            self._stale = True
        if item.is_error and not parts:
            message = item.error or item.terminal_reason or "unknown error"
            lowered = message.lower()
            if "login" in lowered or "authentication" in lowered:
                raise ProviderUnavailableError(PROVIDER_ID, message)
            raise ProviderError(PROVIDER_ID, message)
        self.failures = 0
        session.seen.add(_key(Message(role="assistant", content=text)))
        cost = session.cost_delta(item.cost_usd)
        if self._cfg.max_budget_usd > 0 and cost is not None and cost > self._cfg.max_budget_usd:
            log.warning("claude_persistent.turn_over_budget", cost_usd=cost)
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
