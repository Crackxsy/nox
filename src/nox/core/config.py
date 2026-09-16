"""Four-layer configuration (ADR-010): defaults.yaml -> user.yaml -> profiles/<id>.yaml -> runtime
overrides.

`NoxConfig` mirrors `config/defaults.yaml`; every layer is deep-merged (dicts recursively, lists
and scalars replaced) and the merged result is validated once per layer. An invalid user or
profile layer is discarded with a `ConfigWarning` (Runtime Lifecycle step 1: "invalid user config
-> fall back to defaults + notify"). Runtime overrides live only in memory; an invalid override
raises `ConfigError` because it is a programming or dashboard input error the caller must surface.
`security.hard_prohibitions` may only extend `nox.security.hardlist.HARD_PROHIBITIONS`. Path
values expand `${VAR}` / `%VAR%` / `~`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from nox.security.hardlist import HARD_PROHIBITIONS


class ConfigError(Exception):
    """Raised when the defaults layer or a runtime override is invalid (never for user/profile
    files)."""


class ConfigWarning(BaseModel):
    """A non-fatal problem with one configuration layer.

    `layer` is defaults|user|profile|override.
    """

    model_config = ConfigDict(frozen=True)
    layer: str
    source: str
    message: str

    def __str__(self) -> str:
        return f"[{self.layer}] {self.source}: {self.message}"


class _Strict(BaseModel):
    """Base for all config sections: unknown keys are errors so typos are reported with their
    path."""

    model_config = ConfigDict(extra="forbid", validate_default=True)


# ---- identity ------------------------------------------------------------------------------------


class LocaleConfig(_Strict):
    time_format: str = "24h"
    date_format: str = "DD.MM.YYYY"
    timezone: str = "Europe/Berlin"
    units: Literal["metric", "imperial"] = "metric"


class IdentityConfig(_Strict):
    name: str = "Nox"
    user_display_name: str = ""
    ui_language: Literal["de", "en"] = "de"
    speech_language: Literal["de", "en", "auto"] = "auto"
    locale: LocaleConfig = Field(default_factory=LocaleConfig)


# ---- paths ---------------------------------------------------------------------------------------


def expand_path(value: str) -> Path:
    """Expand `${VAR}`, `$VAR`, `%VAR%` and `~`. Unknown variables are left untouched."""
    return Path(os.path.expanduser(os.path.expandvars(value)))


#: Sentinel default: "derive this path from `paths.data_dir`" (resolved by the after-validators
#: below), so a user who only picks a data folder in `nox onboard` gets everything under it.
DERIVED = Path("<derived>")


class PathsConfig(_Strict):
    data_dir: Path = Path("${APPDATA}/Nox")
    vault_dir: Path = Path("${APPDATA}/Nox/vault")
    index_dir: Path = DERIVED
    database_dir: Path = DERIVED
    cache_dir: Path = DERIVED
    backups_dir: Path = DERIVED
    runtime_dir: Path = Path("${APPDATA}/Nox/runtime")
    logs_dir: Path = Path("${APPDATA}/Nox/logs")

    @model_validator(mode="after")
    def _derive_from_data_dir(self) -> PathsConfig:
        for name in ("index", "database", "cache", "backups"):
            if getattr(self, f"{name}_dir") == DERIVED:
                setattr(self, f"{name}_dir", self.data_dir / name)
        return self

    @field_validator("*", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("path must not be empty")
            return expand_path(value)
        if isinstance(value, Path):
            return expand_path(str(value))
        return value


# ---- security ------------------------------------------------------------------------------------


class AuditConfig(_Strict):
    enabled: bool = True
    retention_days_security: int = Field(default=730, ge=1)
    retention_days_normal: int = Field(default=14, ge=1)


SecurityProfileId = Literal[
    "companion", "coding", "stream", "research", "work", "offline", "rocket_league"
]

#: Loopback services reachable while privacy mode is PRIVATE or OFFLINE (OP-7 C): Ollama only.
DEFAULT_LOOPBACK_ALLOWLIST: tuple[str, ...] = ("127.0.0.1:11434",)


class SecurityConfig(_Strict):
    profile: SecurityProfileId = "companion"
    pin_required_for_security_changes: bool = True
    hard_prohibitions: list[str] = Field(default_factory=lambda: sorted(HARD_PROHIBITIONS))
    # Global egress allow-list (`host`, `host:port`, `*.example.com:443`). A profile with its own
    # `egress_allowlist` (or `cloud_allowed: false`) uses that list instead of this one.
    egress_allowlist: list[str] = Field(default_factory=list)
    # Loopback services that stay reachable in PRIVATE/OFFLINE; profiles may add entries (OP-7 C).
    loopback_allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_LOOPBACK_ALLOWLIST))
    audit: AuditConfig = Field(default_factory=AuditConfig)

    @field_validator("hard_prohibitions")
    @classmethod
    def _must_contain_hardlist(cls, value: list[str]) -> list[str]:
        missing = HARD_PROHIBITIONS.difference(value)
        if missing:
            raise ValueError(
                "hard_prohibitions may only add entries; missing constant entries: "
                + ", ".join(sorted(missing))
            )
        # stable order, no duplicates
        return sorted(set(value))


# ---- privacy -------------------------------------------------------------------------------------


class CaptureConfig(_Strict):
    microphone: bool = True
    camera: bool = False
    screen: bool = True


class RetentionConfig(_Strict):
    raw_transcripts_days: int = Field(default=7, ge=0)
    logs_days: int = Field(default=14, ge=1)
    metrics_days: int = Field(default=365, ge=1)
    viewer_data_inactive_months: int = Field(default=12, ge=1)


class PrivacyConfig(_Strict):
    mode: Literal["full", "balanced", "private", "offline"] = "balanced"
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    zones: list[str] = Field(default_factory=list)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


# ---- ipc -----------------------------------------------------------------------------------------


class IpcConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=47800, ge=1024, le=65535)
    http_port: int = Field(default=47801, ge=1024, le=65535)
    schema_version: int = Field(default=1, ge=1)
    auth: Literal["token"] = "token"


# ---- voice ---------------------------------------------------------------------------------------


class SttConfig(_Strict):
    engine: str = "faster-whisper"
    model: str = "small"
    device: str = "cpu"
    language: str = "auto"
    vad: bool = True
    wake_word: str = "Nox"
    push_to_talk_hotkey: str = "ctrl+alt+space"


class TtsConfig(_Strict):
    engine: str = "piper"
    voice: str = ""
    rate: float = Field(default=1.0, gt=0.0, le=4.0)
    volume: float = Field(default=0.8, ge=0.0, le=1.0)
    streaming: bool = True


class VoiceChannelsConfig(_Strict):
    private_device: str = ""
    stream_device: str = ""
    routing: Literal["private", "stream", "both", "mute"] = "private"


class VoiceConfig(_Strict):
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    channels: VoiceChannelsConfig = Field(default_factory=VoiceChannelsConfig)
    barge_in: bool = True


# ---- ai ------------------------------------------------------------------------------------------


class AiRouterConfig(_Strict):
    default_reasoner: str = "claude_code"
    fallback_chain: list[str] = Field(default_factory=lambda: ["claude_code", "ollama", "rules"])
    background_budget_share: float = Field(default=0.30, ge=0.0, le=1.0)
    reserve_for_stream: bool = True
    roles: dict[str, list[str]] = Field(default_factory=dict)  # per-role provider chains (SP-01)
    health_ttl_s: float = Field(default=60.0, gt=0.0)


class ClaudeCodeProviderConfig(_Strict):
    enabled: bool = True
    command: str = "claude"
    timeout_s: float = Field(default=120.0, gt=0.0)
    model: str = ""  # empty = CLI default; alias (sonnet, haiku) or full id
    safe_mode: bool = True  # --safe-mode: no user hooks/plugins/MCP in Nox requests
    max_budget_usd: float = Field(default=0.0, ge=0.0)
    health_roundtrip: bool = True  # health() does a 1-token request (costs quota)


class OllamaProviderConfig(_Strict):
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


class AiProvidersConfig(_Strict):
    claude_code: ClaudeCodeProviderConfig = Field(default_factory=ClaudeCodeProviderConfig)
    ollama: OllamaProviderConfig = Field(default_factory=OllamaProviderConfig)


class AiConfig(_Strict):
    router: AiRouterConfig = Field(default_factory=AiRouterConfig)
    providers: AiProvidersConfig = Field(default_factory=AiProvidersConfig)


# ---- pet -----------------------------------------------------------------------------------------


class PetConfig(_Strict):
    renderer: Literal["web"] = "web"
    default_monitor: str = "left"
    always_on_top: bool = True
    click_through_when_idle: bool = False
    fps_target: int = Field(default=60, ge=1, le=240)
    fps_in_game: int = Field(default=20, ge=1, le=240)
    greeting_enabled: bool = True
    # OP-10 (decided B): block unsolicited speech (greeting/proactive, never `reply`) while
    # privacy mode is PRIVATE or OFFLINE - the pet stays silent (just shows a "ready" expression).
    quiet_in_private_modes: bool = True
    # OP-1: the chosen procedural creature concept (ui/pet/src/variants); "neutral" is the abstract
    # placeholder (D31) until the PO picks one of the 4 rendered candidates. The shell passes this
    # through to the pet page as `?variant=` (shell/logic.py::pet_url), never a secret.
    variant: str = "neutral"


# ---- attention -----------------------------------------------------------------------------------


class QuietHoursConfig(_Strict):
    start: str = Field(default="23:00", pattern=r"^\d{2}:\d{2}$")
    end: str = Field(default="08:00", pattern=r"^\d{2}:\d{2}$")


class AttentionConfig(_Strict):
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


# ---- stream (Spec v0.2 "Stream Bot", EPIC-11) ------------------------------------------------


class FunkenConfig(_Strict):
    """Funken v1 earn rates and loyalty-tier thresholds (Spec v0.2 §3.5/§9 `twitch.funken.award`).

    Every number here is a conservative placeholder behind config, not a product decision -
    pending approval (Spec v0.2 §3.5) for the shipped values; the earn/spend/tier *mechanism*
    itself is decided (FR-9.17), only the tuning is open.
    """

    earn_per_message: float = Field(default=1.0, ge=0.0)  # pending approval (Spec v0.2 §3.5)
    earn_per_sub: float = Field(default=50.0, ge=0.0)  # pending approval (Spec v0.2 §3.5)
    earn_per_bit: float = Field(default=0.1, ge=0.0)  # pending approval (Spec v0.2 §3.5)
    earn_per_raid: float = Field(default=20.0, ge=0.0)  # pending approval (Spec v0.2 §3.5)
    earn_cooldown_s: float = Field(default=30.0, gt=0.0)  # anti-farming, FR-9.17
    # Additive (Stream Bot core, EPIC-11 ST-11-04/05): per-viewer daily earn cap, anti-farming
    # across an entire session rather than just message-to-message; 0 = uncapped. Placeholder,
    # pending approval like the rates above.
    earn_daily_cap: float = Field(default=200.0, ge=0.0)
    # Five loyalty tiers (PRD backlog default); thresholds are cumulative Funken balances.
    tier_names: list[str] = Field(
        default_factory=lambda: ["newcomer", "regular", "supporter", "veteran", "legend"]
    )  # pending approval (Spec v0.2 §3.5)
    tier_thresholds: list[float] = Field(
        default_factory=lambda: [0.0, 100.0, 500.0, 2000.0, 10000.0]
    )  # pending approval (Spec v0.2 §3.5)

    @field_validator("tier_thresholds")
    @classmethod
    def _same_length_as_names(cls, value: list[float], info: Any) -> list[float]:
        names = info.data.get("tier_names")
        if names is not None and len(value) != len(names):
            raise ValueError("tier_thresholds must have exactly one entry per tier_names")
        return value


class RelevanceConfig(_Strict):
    """Chat relevance/cadence defaults (Spec v0.2 §3.3/§5)."""

    # "roughly one unsolicited public message per two minutes" (Spec v0.2 §3.3)
    unsolicited_interval_s: float = Field(default=120.0, gt=0.0)
    mention_cooldown_s: float = Field(default=5.0, ge=0.0)
    # Additive (Stream Bot core, EPIC-11 ST-11-04/09): `StreamResponder`'s trigger threshold for a
    # non-addressed message (`TwitchChatMessage.relevance`, 0-1) and its own reply cadence limits -
    # separate from `mention_cooldown_s`, which is a relevance-classifier concept upstream.
    # Placeholder, pending approval like the rest of this config.
    threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    channel_cooldown_s: float = Field(default=20.0, ge=0.0)
    max_replies_per_minute: int = Field(default=6, ge=1)


class StreamChatConfig(_Strict):
    # Spec v0.2 §7: recommended 7 days, mirroring privacy.retention.raw_transcripts_days;
    # pending approval for the exact number.
    retain_raw_text_days: int = Field(default=7, ge=0)  # pending approval (Spec v0.2 §7)


class StreamTwitchConfig(_Strict):
    """The Twitch channel Nox joins. Lives here (not only in `plugins/twitch/manifest.yaml`)
    because it is a user setting the dashboard edits through `config.set`, while the manifest
    block holds the plugin's own technical knobs. The plugin reads the manifest value first and
    falls back to this one when the manifest leaves it empty (the shipped default)."""

    channel: str = ""


class StreamConfig(_Strict):
    funken: FunkenConfig = Field(default_factory=FunkenConfig)
    chat: StreamChatConfig = Field(default_factory=StreamChatConfig)
    relevance: RelevanceConfig = Field(default_factory=RelevanceConfig)
    twitch: StreamTwitchConfig = Field(default_factory=StreamTwitchConfig)


# ---- health / logging / supervisor ---------------------------------------------------------------


class HealthConfig(_Strict):
    check_interval_s: float = Field(default=30.0, gt=0.0)
    checkpoint_interval_s: float = Field(default=5.0, gt=0.0)


class LoggingConfig(_Strict):
    model_config = ConfigDict(extra="forbid", validate_default=True, populate_by_name=True)

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = Field(default=True, alias="json")  # yaml key `json`
    pii_filter: bool = True
    retention_days: int | None = Field(default=None, ge=1)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class PluginsConfig(_Strict):
    """ST-11-01: which plugins the core starts, and where the `<id>/manifest.yaml` folders live."""

    enabled: list[str] = Field(default_factory=list)  # plugin ids; empty = no plugin is started
    dir: Path | None = None  # None = <repo>/plugins

    @field_validator("dir", mode="before")
    @classmethod
    def _expand_dir(cls, value: Any) -> Any:
        if isinstance(value, str):
            return expand_path(value) if value.strip() else None
        return value


class SupervisorConfig(_Strict):
    restart_limit: int = Field(default=3, ge=0)
    restart_window_s: float = Field(default=300.0, gt=0.0)
    kill_switch_hotkey: str = "ctrl+alt+shift+k"
    control_host: str = "127.0.0.1"
    control_port: int = Field(default=47799, ge=1024, le=65535)
    heartbeat_interval_s: float = Field(default=2.0, gt=0.0)
    #: Cold boots take 20-40 s on a busy machine: no missed-heartbeat accounting until the core
    #: sent its first heartbeat or this grace since spawn elapsed (2026-09-16 safe-mode incident).
    boot_grace_s: float = Field(default=90.0, gt=0.0)
    missed_for_graceful: int = Field(default=5, ge=1)
    missed_for_hard: int = Field(default=10, ge=1)
    kill_ack_timeout_s: float = Field(default=2.0, gt=0.0)
    stop_timeout_s: float = Field(default=6.0, gt=0.0)
    core_command: list[str] = Field(default_factory=lambda: ["python", "-m", "nox.app"])
    shell_command: list[str] = Field(default_factory=lambda: ["python", "-m", "nox.shell"])
    shell_enabled: bool = True


# ---- Rocket League Stage 1 (Spec v0.3 EPIC-12, ST-12-xx) -----------------------------------------


class RlDetectionConfig(_Strict):
    """Game detection (ST-12-01): process-list polling only, no memory read/injection."""

    poll_interval_s: float = Field(default=2.0, gt=0.0)
    process_names: list[str] = Field(default_factory=lambda: ["RocketLeague.exe"])


class RlReplayConfig(_Strict):
    """Replay watcher (ST-12-02). `folder=None` resolves the OneDrive-redirected Documents special
    folder at runtime (Spec §6.5/§15 - the literal path differs per machine); set explicitly only
    for tests or a non-default install."""

    folder: Path | None = None
    backfill_batch_size: int = Field(default=25, ge=1)
    stable_check_interval_s: float = Field(default=2.0, gt=0.0)
    stable_checks: int = Field(default=2, ge=1)  # consecutive stable size/mtime reads before queue

    @field_validator("folder", mode="before")
    @classmethod
    def _expand_folder(cls, value: Any) -> Any:
        if isinstance(value, str):
            return expand_path(value) if value.strip() else None
        return value


class RlBudgetConfig(_Strict):
    """Vision pipeline budget guard (FR-10.4/NFR-3): capture_hz/region set reduce automatically
    before the ceiling is hit (NFR-5 degradation chain), never crash or silently exceed it. The
    percentages are the spec's target ceiling; ST-12-05 records the actual SP-04 measurement back
    into the spike note, not here."""

    max_gpu_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_fps_loss_pct: float = Field(default=5.0, gt=0.0, le=100.0)
    capture_hz: float = Field(default=2.0, gt=0.0)
    min_capture_hz: float = Field(default=0.5, gt=0.0)


class RlCalloutConfig(_Strict):
    """Prepared-clip callout engine (ST-12-06): rate/cooldown/confidence gates (F131, A129,
    A131)."""

    min_per_minute: int = Field(default=2, ge=0)
    max_per_minute: int = Field(default=4, ge=1)
    cooldown_s: float = Field(default=20.0, ge=0.0)
    boost_low_threshold: int = Field(default=20, ge=0, le=100)
    demo_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)  # silence over guessing (A129)


class RlRetentionConfig(_Strict):
    """Spec §7/§15: retention is pending approval; these mirror the coaching-memory-appropriate
    recommendation (not the short-lived raw-transcript pattern), exact numbers still open."""

    events_days: int = Field(default=180, ge=0)  # pending approval (Spec §7/§15)
    matches_days: int = Field(default=365, ge=0)  # pending approval (Spec §7/§15)


class RlVisionConfig(_Strict):
    """Vision Stage 2 (Spec v0.9 Vision Stage 2, EPIC-18, ST-18-01..06). Opt-in-by-default (spec
    §12 recommendation, pending PO confirmation): the GTX 1060 budget risk (OP-E) is real and
    unresolved, so Stage 2 never starts just because `rl` is enabled. `backend="none"` is the safe
    default even when `enabled=true` was set without also picking a real backend."""

    enabled: bool = False  # opt-in-by-default (Spec §12 open point, not yet PO-confirmed)
    backend: Literal["none", "opencv", "onnx"] = "none"
    sample_hz: float = Field(default=1.0, gt=0.0)  # low rate (Spec §4.2 step 2), shares no budget
    #: Confidence threshold default is an open point (Spec §12); SP-19's measured false-positive
    #: rate on synthetic frames is the only data point behind this number - PO sign-off pending.
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Cost proxy (measured processing time / sample interval) rather than a live GPU/FPS counter -
    #: no PresentMon/pynvml dependency exists yet (SP-05 gates that); FR-10.4's ~10%/~5% targets are
    #: approximated by this fraction, per ST-18-05's "confirm the extension point at build time".
    max_process_fraction: float = Field(default=0.5, gt=0.0, le=1.0)
    consecutive_over_budget: int = Field(default=3, ge=1)
    cooldown_s: float = Field(default=60.0, ge=0.0)
    #: Spec §7 open point's proposed default: detections are not durable gameplay logs - a short
    #: bounded window only, long enough for the post-match rotation analysis to still see them.
    detections_retain_hours: float = Field(default=6.0, ge=0.0)


class RlConfig(_Strict):
    """Rocket League Stage 1 (Spec v0.3 Rocket League Stage 1, EPIC-12, ST-12-01..08)."""

    detection: RlDetectionConfig = Field(default_factory=RlDetectionConfig)
    replay: RlReplayConfig = Field(default_factory=RlReplayConfig)
    budget: RlBudgetConfig = Field(default_factory=RlBudgetConfig)
    callouts: RlCalloutConfig = Field(default_factory=RlCalloutConfig)
    retention: RlRetentionConfig = Field(default_factory=RlRetentionConfig)
    vision: RlVisionConfig = Field(default_factory=RlVisionConfig)


# ---- root ----------------------------------------------------------------------------------------


class RemoteNotificationsConfig(_Strict):
    """Which core events the paired phone may be told about (Spec v0.8 §7). Deliberately a short
    allow-list, not a denylist: an event that is not named here is never forwarded. Every entry is
    still filtered by quiet hours and privacy zones before it leaves the machine."""

    enabled: bool = True
    events: list[str] = Field(
        default_factory=lambda: [
            "security.kill_switch",
            "system.health_changed",
            "stream.started",
            "stream.ended",
            "pm.focus_changed",
        ]
    )


class RemoteConfig(_Strict):
    """Mobile Companion (Spec v0.8, EPIC-17). Off by default: pairing, remote kill switch and
    remote privacy switching only exist once the user turns this on explicitly and pairs a
    device."""

    enabled: bool = False
    #: One-time pairing code lifetime (Spec §5.2/§9, SP-14: 5 minutes, single use).
    pair_code_ttl_s: float = Field(default=300.0, gt=0.0)
    max_devices: int = Field(default=3, ge=1, le=20)
    #: Per-sender command budget; a compromised phone is a likelier event than a local session.
    rate_limit_per_minute: int = Field(default=20, ge=1)
    rate_limit_burst: int = Field(default=5, ge=1)
    #: Chat from the phone goes through the normal orchestrator (same personality, `speak=False`).
    chat_enabled: bool = True
    notifications: RemoteNotificationsConfig = Field(default_factory=RemoteNotificationsConfig)


class ClipsConfig(_Strict):
    """Clip Pipeline (Spec v0.6 §6/§9/§11, EPIC-15). Filesystem roots are distinct from the OBS
    recording root (ES-03), which this pipeline only ever reads: `watch_dir` is the OBS
    replay-buffer output folder (read-only), `library_root`/`export_root`/`quarantine_root` are the
    only write roots. ffmpeg is not currently installed (§11) - `TrimBackend.available()` probes
    `shutil.which("ffmpeg")` at call time rather than assuming a bundled binary."""

    #: Default: `<paths.data_dir>/data/clips/<name>` (resolved in `NoxConfig`).
    library_root: Path = DERIVED
    export_root: Path = DERIVED
    quarantine_root: Path = DERIVED
    #: OBS's configured replay-buffer output folder (set it to your own OBS output folder;
    #: pending approval per Plan v0.6). Default: `<paths.data_dir>/data/clips/incoming`.
    watch_dir: Path = DERIVED
    #: `rl.event.kind` values that qualify as a highlight (Spec v0.6 §4.1); pending calibration.
    highlight_kinds: list[str] = Field(default_factory=lambda: ["goal", "save", "win", "overtime"])
    chat_hype_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    #: Per-`trigger_kind` cooldown (Spec v0.6 §9: "1 per event kind per 15s"), anti-double-clip.
    cooldown_s: float = Field(default=15.0, gt=0.0)
    #: A manual trigger (`!clip`/hotkey) attaches any `rl.event` seen in this preceding window as a
    #: secondary tag (Spec v0.6 §4.2).
    manual_lookback_s: float = Field(default=20.0, ge=0.0)
    watch_poll_interval_s: float = Field(default=2.0, gt=0.0)
    #: How long the watcher waits for a file it does not yet know about before giving up on a
    #: `clip.requested` whose `obs.replay_buffer.save` call itself did not resolve a path.
    watch_timeout_s: float = Field(default=30.0, gt=0.0)
    #: FR-14.1 quarantine-only deletion: discarded clips are moved here, never hard-deleted.
    quarantine_days: int = Field(default=30, ge=1)

    @field_validator("library_root", "export_root", "quarantine_root", "watch_dir", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("path must not be empty")
            return expand_path(value)
        if isinstance(value, Path):
            return expand_path(str(value))
        return value


class PmConfig(_Strict):
    """EPIC-13 Project Management (ST-13-01 subset): vault folders the PM repository reads/writes,
    and the watcher/focus tuning knobs. No new plugin - core feature (Spec v0.4 §6.1)."""

    epics_dir: str = "08 - Epics"
    stories_dir: str = "09 - Stories"
    #: Single "projects list note" (this vault has no per-project folder yet, unlike Epics/
    #: Stories): frontmatter carries a `projects: [{id, name, status, ...}]` list.
    projects_note: str = "Projects.md"
    watch_debounce_s: float = Field(default=1.5, gt=0.0)
    focus_max_items: int = Field(default=5, ge=1)


class AppPattern(_Strict):
    """One process/window-title match rule for `CreativeConfig.app_patterns` (Spec v0.7 §3.1
    step 2). Both fields are optional glob patterns (`fnmatch`, case-insensitive); when both are
    set the match requires both to hold (used for the browser-DAW family: a browser process AND a
    window-title pattern, Spec v0.7 §11 open point)."""

    process: str | None = None
    title: str | None = None


#: Conservative defaults (Spec v0.7 §2/§11) - false negatives (app not detected) are safer than
#: false positives, per the spec's own risk note. All pending approval; override via
#: `config.creative.app_patterns` without a code change.
DEFAULT_CREATIVE_APP_PATTERNS: dict[str, list[dict[str, str]]] = {
    "blender": [{"process": "blender.exe"}],
    "fl_studio": [{"process": "fl64.exe"}, {"process": "fl.exe"}],
    "krita": [{"process": "krita.exe"}],
    "capcut": [{"process": "capcut.exe"}],
    # Not installed on the reference machine as of the Spec v0.7 interview pass;
    # speculative/config-only.
    "davinci_resolve": [{"process": "resolve.exe"}],
    # A browser-hosted DAW (Spec v0.7 §11 open point, resolved here as "yes, detect it"): a
    # Chromium/Firefox process whose window title matches the pattern below - replace the pattern
    # with your own project's window title.
    "browser_daw": [
        {"process": "chrome.exe", "title": "*Web DAW*"},
        {"process": "msedge.exe", "title": "*Web DAW*"},
        {"process": "firefox.exe", "title": "*Web DAW*"},
    ],
}


class CreativeConfig(_Strict):
    """Creative Apps (Spec v0.7 Creative Apps, EPIC-16; pending approval - shipped as
    configurable defaults per the relayed PO sign-off, 2026-09-14, so approval only needs a
    value/config change, never code). `app_patterns`/`hysteresis_s` are the reference defaults;
    the `creative` plugin's own `manifest.yaml` `config:` block is what the plugin worker process
    actually reads (plugins do not read `NoxConfig` directly) - keep the two in sync by hand until
    a shared-defaults loader exists."""

    #: Sustained-match window before a mode switch fires (Spec v0.7 §3.1/§11); pending approval.
    hysteresis_s: float = Field(default=15.0, gt=0.0)
    #: Vault folder for creative project notes (Spec v0.7 §3.4/§11); pending approval.
    vault_folder: str = "16 - Creative Projects"
    app_patterns: dict[str, list[AppPattern]] = Field(
        default_factory=lambda: {
            family: [AppPattern(**entry) for entry in entries]
            for family, entries in DEFAULT_CREATIVE_APP_PATTERNS.items()
        }
    )


class SensorForegroundConfig(_Strict):
    poll_interval_s: float = Field(default=1.0, gt=0.0)


class SensorIdleConfig(_Strict):
    poll_interval_s: float = Field(default=5.0, gt=0.0)
    #: FR-14.4 "~10/20 min" two-stage away detection.
    idle_after_s: float = Field(default=600.0, gt=0.0)
    away_after_s: float = Field(default=1200.0, gt=0.0)


class SensorResourcesConfig(_Strict):
    poll_interval_s: float = Field(default=5.0, gt=0.0)
    #: Adaptive sampling (ST-20-02): slower while idle, faster once a threshold is approached.
    idle_poll_interval_s: float = Field(default=15.0, gt=0.0)
    cpu_high_watermark_pct: float = Field(default=80.0, ge=0.0, le=100.0)
    gpu_enabled: bool = True


class SensorGameConfig(_Strict):
    """Generic process-lifecycle hook (`sensor.process_started`/`sensor.process_ended`) a game
    plugin (e.g. `rl`) subscribes to instead of watching processes itself."""

    poll_interval_s: float = Field(default=5.0, gt=0.0)
    process_names: list[str] = Field(default_factory=lambda: ["RocketLeague.exe"])


class SensorsConfig(_Strict):
    """PC Awareness Sensors (Spec v0.5 §3.5, EPIC-20, ST-20-01..07 subset delivered in this pass:
    foreground/zone, idle/away, resource sampling, generic game-process hook. No dedicated
    `plugins/system/` worker in this pass - a core service is enough for what these stories need;
    ST-20-03's persisted 24h/30d/1y history store is out of scope here (no migration in this
    change), samples live only in an in-memory ring buffer for `sensors.status.read`."""

    enabled: bool = True
    foreground: SensorForegroundConfig = Field(default_factory=SensorForegroundConfig)
    idle: SensorIdleConfig = Field(default_factory=SensorIdleConfig)
    resources: SensorResourcesConfig = Field(default_factory=SensorResourcesConfig)
    game: SensorGameConfig = Field(default_factory=SensorGameConfig)
    #: Ring-buffer depth per sensor for `sensors.status.read` (samples, not a time window).
    history_len: int = Field(default=120, ge=1)


class ProactiveConfig(_Strict):
    """EPIC-19 (ST-19-04..08): knobs for the notification/attention layer that sit alongside, and
    reuse, `AttentionConfig` (quiet hours, proactivity_level, per_mode, interruptions_per_hour
    already live there - not duplicated here). No migration in this change: the notification store
    is an in-memory bounded buffer (`nox.proactive.store.NotificationStore`), not a DB table."""

    enabled: bool = True
    #: `nox.proactive.store.NotificationStore` ring-buffer depth for `proactive.status.read`.
    notification_store_limit: int = Field(default=200, ge=10, le=2000)
    #: A.13: announce before unsolicited speech, except URGENT itself and time-critical callouts.
    announce_before_speaking: bool = True
    #: Feedback-loop (ST-19-07) multiplier bounds on hint frequency; never applied to URGENT.
    feedback_min_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    feedback_max_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    feedback_step: float = Field(default=0.05, gt=0.0, le=0.5)
    #: ST-19-06: bias hint timing toward historically well-received hours-of-day.
    timing_learner_enabled: bool = True

    @field_validator("feedback_max_weight")
    @classmethod
    def _max_ge_min(cls, value: float, info: Any) -> float:
        min_w = info.data.get("feedback_min_weight")
        if min_w is not None and value < min_w:
            raise ValueError("feedback_max_weight must be >= feedback_min_weight")
        return value


class MemoryConfig(_Strict):
    """EPIC-07 Memory & Vault (ST-07-01..05): vault watcher/indexer, embedding model, and retrieval
    tuning knobs. `paths.vault_dir` stays the single source of truth for the vault location."""

    embed_model: str = "nomic-embed-text"
    vault_watch_debounce_s: float = Field(default=2.0, gt=0.0)
    retrieval_max_tokens: int = Field(default=4000, ge=256)
    retrieval_k: int = Field(default=8, ge=1, le=50)
    full_scan_on_boot: bool = True
    retention_note_version_days: int = Field(default=30, ge=1)


class NoxConfig(_Strict):
    schema_version: int = Field(default=1, ge=1, le=1)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    ipc: IpcConfig = Field(default_factory=IpcConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    pet: PetConfig = Field(default_factory=PetConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    rl: RlConfig = Field(default_factory=RlConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    pm: PmConfig = Field(default_factory=PmConfig)
    creative: CreativeConfig = Field(default_factory=CreativeConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    _warnings: list[ConfigWarning] = PrivateAttr(default_factory=list)
    _profile_id: str | None = PrivateAttr(default=None)

    @property
    def warnings(self) -> list[ConfigWarning]:
        """Problems found while loading (rejected layers).

        Empty when every layer applied cleanly.
        """
        return list(self._warnings)

    @property
    def profile_id(self) -> str | None:
        """The profile layer that was applied, or None."""
        return self._profile_id

    @property
    def log_retention_days(self) -> int:
        return self.logging.retention_days or self.privacy.retention.logs_days

    @model_validator(mode="after")
    def _derive_clip_roots(self) -> NoxConfig:
        base = self.paths.data_dir / "data" / "clips"
        for name, sub in (
            ("library_root", "library"),
            ("export_root", "export"),
            ("quarantine_root", "quarantine"),
            ("watch_dir", "incoming"),
        ):
            if getattr(self.clips, name) == DERIVED:
                setattr(self.clips, name, base / sub)
        return self


# ---- loading -------------------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: nested dicts merged recursively, everything else replaced by `overlay`."""
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def format_validation_error(exc: ValidationError) -> str:
    """One line per error with the dotted config path, e.g. `ipc.port: Input should be ...`."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


def read_yaml_layer(path: Path) -> dict[str, Any]:
    """Parse a YAML mapping.

    Raises ConfigError for unreadable, unparsable or non-mapping content.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _profile_path(profile_id: str, defaults_path: Path, user_path: Path | None) -> Path | None:
    """User profiles (next to user.yaml) shadow repo profiles (next to defaults.yaml)."""
    candidates: list[Path] = []
    if user_path is not None:
        candidates.append(user_path.parent / "profiles" / f"{profile_id}.yaml")
    candidates.append(defaults_path.parent / "profiles" / f"{profile_id}.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(
    defaults_path: Path,
    user_path: Path | None = None,
    profile_id: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> NoxConfig:
    """Load and merge the four layers. See module docstring for the fallback rules.

    Warnings for rejected user/profile layers are available on the returned config (`.warnings`).
    """
    warnings: list[ConfigWarning] = []

    try:
        merged = read_yaml_layer(defaults_path)
        NoxConfig.model_validate(merged)
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(
            f"defaults invalid ({defaults_path}): {format_validation_error(exc)}"
        ) from exc

    def apply_layer(layer: str, source: Path, data: dict[str, Any]) -> None:
        nonlocal merged
        candidate = deep_merge(merged, data)
        try:
            NoxConfig.model_validate(candidate)
        except ValidationError as exc:
            warnings.append(
                ConfigWarning(layer=layer, source=str(source), message=format_validation_error(exc))
            )
            return
        merged = candidate

    if user_path is not None:
        if user_path.is_file():
            try:
                apply_layer("user", user_path, read_yaml_layer(user_path))
            except ConfigError as exc:
                warnings.append(
                    ConfigWarning(layer="user", source=str(user_path), message=str(exc))
                )
        else:
            warnings.append(
                ConfigWarning(
                    layer="user", source=str(user_path), message="file not found, using defaults"
                )
            )

    applied_profile: str | None = None
    if profile_id:
        path = _profile_path(profile_id, defaults_path, user_path)
        if path is None:
            warnings.append(
                ConfigWarning(layer="profile", source=profile_id, message="profile file not found")
            )
        else:
            before = len(warnings)
            try:
                apply_layer("profile", path, read_yaml_layer(path))
            except ConfigError as exc:
                warnings.append(ConfigWarning(layer="profile", source=str(path), message=str(exc)))
            if len(warnings) == before:
                applied_profile = profile_id

    if overrides:
        candidate = deep_merge(merged, overrides)
        try:
            NoxConfig.model_validate(candidate)
        except ValidationError as exc:
            raise ConfigError(f"runtime override invalid: {format_validation_error(exc)}") from exc
        merged = candidate

    config = NoxConfig.model_validate(merged)
    config._warnings = warnings
    config._profile_id = applied_profile
    return config
