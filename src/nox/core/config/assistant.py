"""Sections that tune the assistant itself: voice, language models, the pet, attention, memory,
the proactive layer and the local sensors.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from nox.core.config.types import StrictSection, require_ordered

__all__ = [
    "AiConfig",
    "AiProvidersConfig",
    "AiRouterConfig",
    "AttentionConfig",
    "ClaudeCodeProviderConfig",
    "MemoryConfig",
    "OllamaProviderConfig",
    "PetConfig",
    "ProactiveConfig",
    "QuietHoursConfig",
    "SensorForegroundConfig",
    "SensorGameConfig",
    "SensorIdleConfig",
    "SensorResourcesConfig",
    "SensorsConfig",
    "SttConfig",
    "TtsConfig",
    "VoiceChannelsConfig",
    "VoiceConfig",
]


# ---- voice --------------------------------------------------------------------------------------


class SttConfig(StrictSection):
    engine: str = "faster-whisper"
    model: str = "small"
    device: str = "cpu"
    language: str = "auto"
    vad: bool = True
    wake_word: str = "Nox"
    push_to_talk_hotkey: str = "ctrl+alt+space"
    #: `continuous` keeps the microphone open behind the wake-word gate; `ptt_only` opens it only
    #: while push-to-talk is held.
    listening_mode: Literal["continuous", "ptt_only"] = "continuous"
    #: `openwakeword` is used when the package and a model file are both available, and falls back
    #: to `text` - matching on the transcript - with a `limited` health reason otherwise.
    wake_word_engine: Literal["openwakeword", "text"] = "openwakeword"
    #: Model file for the acoustic detector, relative to `<models_dir>/openwakeword` or absolute.
    #: Empty = every model file in that directory. openWakeWord ships no model for "Nox".
    wake_word_model: str = ""
    wake_word_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    #: How long a detection keeps the gate open for the utterance that follows it.
    wake_window_s: float = Field(default=8.0, gt=0.0)
    #: After Nox was addressed, follow-up questions may skip the wake word for this long.
    conversation_window_s: float = Field(default=20.0, ge=0.0)
    #: Keeps the voice kill phrase reachable without an acoustic model for it: short segments are
    #: still transcribed, checked for the kill phrase and then discarded, never reported.
    kill_phrase_watchdog: bool = True
    kill_watchdog_max_ms: int = Field(default=2500, ge=0)


class TtsConfig(StrictSection):
    engine: Literal["piper", "kokoro"] = "piper"
    voice: str = ""
    rate: float = Field(default=1.0, gt=0.0, le=4.0)
    volume: float = Field(default=0.8, ge=0.0, le=1.0)
    streaming: bool = True


class VoiceChannelsConfig(StrictSection):
    private_device: str = ""
    stream_device: str = ""
    routing: Literal["private", "stream", "both", "mute"] = "private"


class VoiceConfig(StrictSection):
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    channels: VoiceChannelsConfig = Field(default_factory=VoiceChannelsConfig)
    barge_in: bool = True
    #: Root for the voice model files (`<models_dir>/piper`, `/kokoro`, `/openwakeword`). Empty
    #: resolves to `<paths.data_dir>/models`. The voice worker only ever receives this section of
    #: the configuration, so a moved `paths.data_dir` has to be repeated here.
    models_dir: str = ""


# ---- language models ----------------------------------------------------------------------------


class AiRouterConfig(StrictSection):
    default_reasoner: str = "claude_code"
    fallback_chain: list[str] = Field(default_factory=lambda: ["claude_code", "ollama", "rules"])
    background_budget_share: float = Field(default=0.30, ge=0.0, le=1.0)
    reserve_for_stream: bool = True
    #: Per-role provider chains, e.g. `chat: [ollama, claude_code, rules]` to answer chat from the
    #: local model first. A role without an entry uses `fallback_chain`.
    roles: dict[str, list[str]] = Field(default_factory=dict)
    health_ttl_s: float = Field(default=60.0, gt=0.0)


class ClaudeCodeProviderConfig(StrictSection):
    enabled: bool = True
    command: str = "claude"
    timeout_s: float = Field(default=120.0, gt=0.0)
    model: str = ""  # empty = the CLI default; an alias (sonnet, haiku) or a full id
    safe_mode: bool = True  # no user hooks, plugins or tool servers in Nox's requests
    max_budget_usd: float = Field(default=0.0, ge=0.0)
    health_roundtrip: bool = True  # health() does a one-token request, which costs quota


class OllamaProviderConfig(StrictSection):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2:3b"
    gpu_allowed_modes: list[str] = Field(
        default_factory=lambda: ["companion", "coding", "research", "idle"]
    )
    timeout_s: float = Field(default=60.0, gt=0.0)

    @field_validator("gpu_allowed_modes")
    @classmethod
    def _never_in_game(cls, value: list[str]) -> list[str]:
        if "rocket_league" in value:
            raise ValueError("ollama GPU use is never allowed in rocket_league mode")
        return value


class AiProvidersConfig(StrictSection):
    claude_code: ClaudeCodeProviderConfig = Field(default_factory=ClaudeCodeProviderConfig)
    ollama: OllamaProviderConfig = Field(default_factory=OllamaProviderConfig)


class AiConfig(StrictSection):
    router: AiRouterConfig = Field(default_factory=AiRouterConfig)
    providers: AiProvidersConfig = Field(default_factory=AiProvidersConfig)


# ---- pet and attention --------------------------------------------------------------------------


class PetConfig(StrictSection):
    renderer: Literal["web"] = "web"
    default_monitor: str = "left"
    always_on_top: bool = True
    click_through_when_idle: bool = False
    fps_target: int = Field(default=60, ge=1, le=240)
    fps_in_game: int = Field(default=20, ge=1, le=240)
    greeting_enabled: bool = True
    #: Block unsolicited speech - the greeting and proactive hints, never a reply to the user -
    #: while privacy mode is private or offline: the pet stays silent and just shows it is ready.
    quiet_in_private_modes: bool = True
    #: Creature shape rendered by the pet window (`ui/pet/src/variants`), or `sprite:<id>` for a
    #: sprite set. "neutral" is the abstract shape that ships with Nox. The shell passes it
    #: through to the pet page as `?variant=`; it is never a secret.
    variant: str = "neutral"


class QuietHoursConfig(StrictSection):
    start: str = Field(default="23:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")


class AttentionConfig(StrictSection):
    proactivity_level: int = Field(default=3, ge=0, le=5)
    per_mode: dict[str, int] = Field(default_factory=dict)
    interruptions_per_hour: int = Field(default=3, ge=0)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)

    @field_validator("per_mode")
    @classmethod
    def _levels_in_range(cls, value: dict[str, int]) -> dict[str, int]:
        for mode, level in value.items():
            if not 0 <= level <= 5:
                raise ValueError(f"per_mode.{mode} must be within 0..5, got {level}")
        return value


# ---- memory, proactivity, sensors ---------------------------------------------------------------


class MemoryConfig(StrictSection):
    """Vault watcher and indexer, the embedding model, and retrieval tuning.

    The vault location itself stays `paths.vault_dir`; it is not repeated here.
    """

    embed_model: str = "nomic-embed-text"
    vault_watch_debounce_s: float = Field(default=2.0, gt=0.0)
    retrieval_max_tokens: int = Field(default=4000, ge=256)
    retrieval_k: int = Field(default=8, ge=1, le=50)
    full_scan_on_boot: bool = True
    retention_note_version_days: int = Field(default=30, ge=1)


class ProactiveConfig(StrictSection):
    """The notification and attention layer.

    Quiet hours, `proactivity_level`, `per_mode` and `interruptions_per_hour` live under
    `attention` and are reused from there rather than duplicated.
    """

    enabled: bool = True
    #: Dismissed or expired notifications older than this are purged from the database.
    notifications_retention_days: int = Field(default=30, ge=1, le=3650)
    #: Ring-buffer depth behind `proactive.status.read` (entries, not a time window).
    notification_store_limit: int = Field(default=200, ge=10, le=2000)
    #: Announce who is speaking before unsolicited speech, except for urgent and time-critical
    #: messages, where the announcement would cost more than it explains.
    announce_before_speaking: bool = True
    #: Bounds on the learned multiplier for hint frequency. Never applied to urgent messages.
    feedback_min_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    feedback_max_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    feedback_step: float = Field(default=0.05, gt=0.0, le=0.5)
    #: Bias hint timing toward hours of the day whose hints were historically well received.
    timing_learner_enabled: bool = True

    @model_validator(mode="after")
    def _weights_ordered(self) -> ProactiveConfig:
        return require_ordered(self, "feedback_min_weight", "feedback_max_weight")


class SensorForegroundConfig(StrictSection):
    poll_interval_s: float = Field(default=1.0, gt=0.0)


class SensorIdleConfig(StrictSection):
    poll_interval_s: float = Field(default=5.0, gt=0.0)
    #: Two-stage away detection: idle first, away once the user has been gone for a while.
    idle_after_s: float = Field(default=600.0, gt=0.0)
    away_after_s: float = Field(default=1200.0, gt=0.0)

    @model_validator(mode="after")
    def _stages_ordered(self) -> SensorIdleConfig:
        return require_ordered(self, "idle_after_s", "away_after_s")


class SensorResourcesConfig(StrictSection):
    poll_interval_s: float = Field(default=5.0, gt=0.0)
    #: Adaptive sampling: slower while the machine is idle, faster once a threshold is approached.
    idle_poll_interval_s: float = Field(default=15.0, gt=0.0)
    cpu_high_watermark_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    gpu_enabled: bool = True


class SensorGameConfig(StrictSection):
    """The generic process-lifecycle hook.

    A game plugin subscribes to `sensor.process_started` and `sensor.process_ended` instead of
    watching the process list itself.
    """

    poll_interval_s: float = Field(default=5.0, gt=0.0)
    process_names: list[str] = Field(default_factory=lambda: ["RocketLeague.exe"])


class SensorsConfig(StrictSection):
    """Local awareness sensors: the foreground window and its privacy zone, idle and away,
    resource load, and the generic game-process hook. Samples live in an in-memory ring buffer,
    not in the database.
    """

    enabled: bool = True
    foreground: SensorForegroundConfig = Field(default_factory=SensorForegroundConfig)
    idle: SensorIdleConfig = Field(default_factory=SensorIdleConfig)
    resources: SensorResourcesConfig = Field(default_factory=SensorResourcesConfig)
    game: SensorGameConfig = Field(default_factory=SensorGameConfig)
    #: Ring-buffer depth per sensor for `sensors.status.read` (samples, not a time window).
    history_len: int = Field(default=120, ge=1)
