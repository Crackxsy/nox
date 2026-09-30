"""Payload models for the core itself: system lifecycle, health, security, voice, the
model router, the pet, plugins and Rocket League."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nox.core.events.base import HealthStatus


class HealthChanged(BaseModel):
    component: str
    status: HealthStatus
    reason: str = ""


class ModeChanged(BaseModel):
    previous: str
    current: str
    layers: list[str] = Field(default_factory=list)
    reason: str = ""
    # a game (e.g. Rocket League) is in the foreground: background work pauses
    game_running: bool = False


class StateChanged(BaseModel):
    path: str  # dotted path in NoxState, e.g. "assistant.mood.energy"
    old: Any = None
    new: Any = None
    version: int


class TranscriptReady(BaseModel):
    text: str
    language: str
    confidence: float = Field(ge=0.0, le=1.0)
    addressed_to_nox: bool
    duration_ms: int
    latency_ms: int


class VoiceKillPhrase(BaseModel):
    """Emitted by the voice worker when "Nox Notaus"/"Nox emergency stop" is heard.

    Never via the LLM.
    """

    by: str = "voice"
    reason: str = "kill phrase"
    language: str = ""


class TtsStarted(BaseModel):
    text: str
    channel: str  # private | stream | both
    engine: str
    utterance_id: str


class AiRequestStarted(BaseModel):
    request_id: str
    provider: str
    role: str  # classify | chat | reason | code | background
    mode: str


class AiResponseReady(BaseModel):
    request_id: str
    provider: str
    text: str
    latency_ms: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    degraded: bool = False
    degraded_reason: str = ""


class AiRequestFailed(BaseModel):
    request_id: str
    provider: str
    error: str
    fallback_to: str | None = None


class PetStateChanged(BaseModel):
    functional: str  # idle|listening|thinking|speaking|working|error|muted|privacy|unavailable
    mood: dict[str, float] = Field(default_factory=dict)
    expression: str = "normal"  # one of the 21 expression states
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)


class PrivacyModeChanged(BaseModel):
    previous: str
    current: str  # full | balanced | private | offline
    by: str  # user | hotkey | telegram | system


class CaptureChanged(BaseModel):
    microphone: bool
    camera: bool
    screen: bool
    cloud: bool


class KillSwitch(BaseModel):
    by: str  # hotkey | tray | ui | telegram | voice | supervisor
    reason: str = ""


class PermissionRequested(BaseModel):
    request_id: str
    agent: str
    tool: str
    action: str
    mode: str
    risk: str
    target: str = ""


class PermissionDecided(BaseModel):
    request_id: str
    decision: str  # allow | confirm | deny
    rule_id: str
    by: str  # policy | user | pin
    reason: str = ""


class AuditEntry(BaseModel):
    seq: int
    actor: str
    tool: str
    action: str
    target: str = ""
    decision: str
    result: str  # ok | denied | failed | aborted
    task_id: str | None = None
    prev_hash: str
    hash: str


class PluginLifecycle(BaseModel):
    plugin_id: str
    version: str
    reason: str = ""


class HealthReport(BaseModel):
    components: dict[str, HealthChanged]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
