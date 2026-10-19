"""AI provider abstraction. No vendor is hard-wired; the router chooses by task role, latency,
privacy
mode, budget, load and availability (PRD). Claude Code's operational model is validated in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from nox.core.events import HealthStatus


class AiRole(StrEnum):
    CLASSIFY = "classify"  # intent, relevance, mood, event priority (rules or tiny local model)
    CHAT = "chat"  # short answers, stream chat
    REASON = "reason"  # complex conversation, planning, architecture
    CODE = "code"  # implementation via Claude Code
    BACKGROUND = "background"  # summaries, replay analysis, vault maintenance


class ToolDefinition(BaseModel):
    """One tool offered to the model for this turn, by its dotted Nox name.

    Providers translate it into their own format (`nox.tools.names` maps the name for the wire).
    `asks_first` tells the model that calling it opens a confirmation dialog for the user.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    asks_first: bool = False


class ToolCallRequest(BaseModel):
    """A tool call the model asked for: the dotted name and its arguments, not yet checked."""

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: str  # system | user | assistant | tool
    content: str
    #: Set on an assistant message that asked for tools (native tool calling).
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    #: Set on a `tool` message: which call this is the result of.
    tool_name: str = ""
    tool_call_id: str = ""


class AiRequest(BaseModel):
    request_id: str
    role: AiRole
    messages: list[Message]
    mode: str = "companion"
    privacy_mode: str = "balanced"
    max_tokens: int = 512
    temperature: float = 0.6
    timeout_s: float = 60.0
    stream: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)
    #: Tools the model may call in this turn. Empty = the model can only talk.
    tools: list[ToolDefinition] = Field(default_factory=list)
    #: Per-turn context (time, what Nox knows about the user, retrieved memory, what is unavailable
    #: right now). Kept apart from the stable system message so a provider with a long-lived
    #: session can send it with the user's message instead of restarting the session.
    context: str = ""
    #: The token of this turn's tool loop, for a provider whose tools run through Nox's MCP bridge.
    #: A credential: never serialized, never shown in a repr or a log line.
    tool_turn: str = Field(default="", exclude=True, repr=False)

    def system_text(self) -> str:
        """System message(s) plus the per-turn context, for providers without a session."""
        parts = [m.content for m in self.messages if m.role == "system"]
        if self.context:
            parts.append(self.context)
        return "\n\n".join(part for part in parts if part)


class AiChunk(BaseModel):
    request_id: str
    delta: str
    done: bool = False
    #: Tool calls for Nox to execute and answer before the model continues (native tool calling
    #: in a loop Nox drives, e.g. Ollama). Arrives on the final chunk of a model step.
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    #: Tools the provider is running itself right now through Nox's bridge (Claude Code + MCP);
    #: informational, the calls are checked and executed by Nox's tool loop all the same.
    tool_activity: list[str] = Field(default_factory=list)


class AiResponse(BaseModel):
    request_id: str
    provider: str
    text: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    degraded: bool = False
    degraded_reason: str = ""
    cost_usd: float | None = None  # provider-reported cost equivalent; None if unknown


class ProviderInfo(BaseModel):
    id: str  # claude_code | ollama | gemini | groq | rules
    display_name: str
    #: The same name in German, for the German user interface. Empty means the English name is
    #: already the German one too (a product name like "Claude Code" is not translated), and the
    #: user interface falls back to `display_name`.
    display_name_de: str = ""
    local: bool  # True = never leaves the machine
    roles: list[AiRole]
    status: HealthStatus = HealthStatus.UNAVAILABLE
    reason: str = ""


class AiProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...
    async def health(self) -> ProviderInfo:
        """Actively verify availability (never assume); cheap enough for the startup report."""
        ...

    async def complete(self, request: AiRequest) -> AiResponse: ...
    def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]: ...


class Router(Protocol):
    """Picks a provider chain for a request and applies fallbacks. Cloud providers are skipped when
    the privacy mode or profile forbids cloud. Emits ai.provider_changed and ai.request_failed."""

    async def complete(self, request: AiRequest) -> AiResponse: ...
    def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]: ...
    async def providers(self) -> list[ProviderInfo]: ...
    def explain(self, request_id: str) -> str:
        """Why this provider was chosen (shown in the dashboard)."""
        ...
