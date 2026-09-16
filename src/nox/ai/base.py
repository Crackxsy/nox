"""AI provider abstraction. No vendor is hard-wired; the router chooses by task role, latency,
privacy mode, budget, load and availability (PRD FR-6.1). Claude Code's operational model is
validated in SP-01.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from nox.core.events import HealthStatus


class AiRole(StrEnum):
    CLASSIFY = "classify"  # intent, relevance, mood, event priority (rules or tiny local model)
    CHAT = "chat"  # short answers, stream chat
    REASON = "reason"  # complex conversation, planning, architecture
    CODE = "code"  # implementation via Claude Code
    BACKGROUND = "background"  # summaries, replay analysis, vault maintenance


class Message(BaseModel):
    role: str  # system | user | assistant
    content: str


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


class AiChunk(BaseModel):
    request_id: str
    delta: str
    done: bool = False


class AiResponse(BaseModel):
    request_id: str
    provider: str
    text: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    degraded: bool = False
    degraded_reason: str = ""
    cost_usd: float | None = None  # provider-reported cost equivalent (FR-6.6); None if unknown


class ProviderInfo(BaseModel):
    id: str  # claude_code | ollama | gemini | groq | rules
    display_name: str
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
