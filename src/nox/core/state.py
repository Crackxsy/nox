"""NoxState: the ultra-fast live state (single source for "what is happening right now").

Contract (see vault 02 - Architecture/Component Model and 03 - Data Model):
- One versioned pydantic tree. Every mutation goes through StateManager.update(path, value) which
  bumps `version`, emits state.changed(path, old, new, version) and schedules a checkpoint.
- Checkpoints every `health.checkpoint_interval_s` (default 5 s) and immediately on important events
  (mode change, kill switch, privacy change, task status). Written to SQLite `state_checkpoints`.
- The vault is the long-term truth; on conflict after a crash the vault wins and live state is
  rebuilt.
- No secrets, no raw audio, no raw transcripts in state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Mode(StrEnum):
    COMPANION = "companion"
    CODING = "coding"
    PROJECT = "project"
    STREAM = "stream"
    ROCKET_LEAGUE = "rocket_league"
    CREATIVE = "creative"
    RESEARCH = "research"
    FOCUS = "focus"
    IDLE = "idle"


class SleepTier(StrEnum):
    NONE = "none"
    NORMAL = "normal"
    HIGH_LOAD = "high_load"
    OFFLINE = "offline"


class PrivacyMode(StrEnum):
    FULL = "full"
    BALANCED = "balanced"
    PRIVATE = "private"
    OFFLINE = "offline"


class SystemLevel(StrEnum):
    RUNNING = "running"
    DEGRADED = "degraded"
    SAFE_MODE = (
        "safe_mode"  # after kill switch: no tools, no agents, no capture, manual restart only
    )
    STOPPING = "stopping"


class PetFunctional(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WORKING = "working"
    ERROR = "error"
    MUTED = "muted"
    PRIVACY = "privacy"
    UNAVAILABLE = "unavailable"


EXPRESSIONS: tuple[str, ...] = (
    "normal",
    "happy",
    "excited",
    "confused",
    "curious",
    "bored",
    "proud",
    "shy",
    "scared",
    "angry",
    "sad",
    "coding",
    "working",
    "thinking",
    "coaching",
    "streaming",
    "celebrating",
    "sleeping",
    "tilted",
    "hype",
    "smug",
)


class Mood(BaseModel):
    """Dimensions 0..1 with individual half-lives (decay handled by the pet service)."""

    mood: float = 0.6
    energy: float = 0.6
    stress: float = 0.2
    curiosity: float = 0.5
    affection: float = 0.5
    attention: float = 0.5


class UserState(BaseModel):
    activity: str = "unknown"  # working | coding | playing | streaming | talking | idle | away
    application: str = ""
    window_title: str = ""
    focus_project: str | None = None
    focus_manual: bool = False
    mood_estimate: dict[str, float] = Field(
        default_factory=dict
    )  # frustration, energy, confidence (0..1)
    present: bool = True
    last_input_at: datetime | None = None


class AssistantState(BaseModel):
    mode: Mode = Mode.COMPANION
    layers: list[str] = Field(default_factory=list)  # combined-mode layers
    mode_locked: bool = False
    sleep: SleepTier = SleepTier.NONE
    pet_functional: PetFunctional = PetFunctional.IDLE
    expression: str = "normal"
    expression_intensity: float = 0.5
    mood: Mood = Field(default_factory=Mood)
    muted: bool = False  # "Klappe halten"
    muted_until_mode_change: bool = False
    current_task: str | None = None
    current_thought: str = ""


class PrivacyState(BaseModel):
    mode: PrivacyMode = PrivacyMode.BALANCED
    microphone: bool = False  # capture active right now
    camera: bool = False
    screen: bool = False
    cloud_request_active: bool = False
    panic: bool = False


class VoiceState(BaseModel):
    stt_engine: str = ""
    tts_engine: str = ""
    listening: bool = False
    speaking: bool = False
    ptt_held: bool = False
    routing: str = "private"  # private | stream | both | mute
    last_transcript_latency_ms: int | None = None


class AiState(BaseModel):
    active_provider: str = ""
    providers: dict[str, str] = Field(
        default_factory=dict
    )  # provider -> available|limited|unavailable
    last_latency_ms: int | None = None
    budget_used_share: float = 0.0
    degraded: bool = False


class SystemState(BaseModel):
    level: SystemLevel = SystemLevel.RUNNING
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    health: dict[str, str] = Field(
        default_factory=dict
    )  # component -> available|limited|unavailable
    online: str = "unknown"  # online | offline | degraded
    cpu: float = 0.0
    gpu: float = 0.0
    ram_mb: float = 0.0
    vram_mb: float = 0.0


class ProjectState(BaseModel):
    active_project: str | None = None
    active_epic: str | None = None
    active_story: str | None = None


class NoxState(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    version: int = 0
    schema_version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    system: SystemState = Field(default_factory=SystemState)
    user: UserState = Field(default_factory=UserState)
    assistant: AssistantState = Field(default_factory=AssistantState)
    privacy: PrivacyState = Field(default_factory=PrivacyState)
    voice: VoiceState = Field(default_factory=VoiceState)
    ai: AiState = Field(default_factory=AiState)
    project: ProjectState = Field(default_factory=ProjectState)


class StateManager(Protocol):
    """Owns the single NoxState instance in the core process."""

    @property
    def state(self) -> NoxState: ...
    def get(self, path: str) -> Any: ...
    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        """Set a dotted path, bump version, emit state.changed, return the new version."""
        ...

    async def checkpoint(self, *, immediate: bool = False) -> None: ...
    async def restore_latest(self) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...
