"""Event catalog v1 and the EventBus contract.

Events are typed by name (dotted) and carry a pydantic payload model registered here.
Add events deliberately; document them in the vault (04 - Event Model). Names are stable once
shipped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class HealthStatus(StrEnum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class Event(BaseModel):
    """In-process event.

    Over IPC it is wrapped in an Envelope(kind=event, name=name, payload=payload).
    """

    model_config = ConfigDict(frozen=True)
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    corr: str | None = None
    source: str = "core"


# ---- Catalog v1 (names). Payload models below. ---------------------------------------------------


class E:
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPING = "system.stopping"
    SYSTEM_HEALTH_CHANGED = "system.health_changed"
    SYSTEM_MODE_CHANGED = "system.mode_changed"
    SYSTEM_HANDLER_FAILED = "system.handler_failed"  # bus: handler raised (event, handler, error)
    STATE_CHANGED = "state.changed"

    VOICE_INPUT_STARTED = "voice.input_started"
    VOICE_INPUT_STOPPED = "voice.input_stopped"
    VOICE_TRANSCRIPT_PARTIAL = "voice.transcript_partial"
    VOICE_TRANSCRIPT_READY = "voice.transcript_ready"
    VOICE_PTT_PRESSED = "voice.ptt_pressed"
    VOICE_PTT_RELEASED = "voice.ptt_released"
    VOICE_MUTED = "voice.muted"
    VOICE_KILL_PHRASE = (
        "voice.kill_phrase"  # local kill phrase heard; core maps it to security.kill
    )

    TTS_STARTED = "tts.started"
    TTS_CHUNK = "tts.chunk"
    TTS_FINISHED = "tts.finished"
    TTS_INTERRUPTED = "tts.interrupted"

    AI_REQUEST_STARTED = "ai.request_started"
    AI_RESPONSE_CHUNK = "ai.response_chunk"
    AI_RESPONSE_READY = "ai.response_ready"
    AI_REQUEST_FAILED = "ai.request_failed"
    AI_PROVIDER_CHANGED = "ai.provider_changed"

    PET_STATE_CHANGED = "pet.state_changed"
    PET_INTERACTION = "pet.interaction"

    PRIVACY_MODE_CHANGED = "privacy.mode_changed"
    PRIVACY_CAPTURE_CHANGED = "privacy.capture_changed"

    SECURITY_KILL_SWITCH = "security.kill_switch"
    SECURITY_PANIC = "security.panic"
    SECURITY_PERMISSION_REQUESTED = "security.permission_requested"
    SECURITY_PERMISSION_DECIDED = "security.permission_decided"
    SECURITY_AUDIT = "security.audit"

    PLUGIN_STARTED = "plugin.started"
    PLUGIN_STOPPED = "plugin.stopped"
    PLUGIN_FAILED = "plugin.failed"

    MEMORY_CREATED = "memory.created"
    MEMORY_DELETED = "memory.deleted"
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"

    IPC_CLIENT_CONNECTED = "ipc.client_connected"
    IPC_CLIENT_DISCONNECTED = "ipc.client_disconnected"

    HEALTH_REPORT = "health.report"

    # Stream Bot (Spec v0.2 §8, EPIC-11); STREAM_STARTED/STREAM_ENDED were reserved earlier without
    # a payload model - this spec defines one. GAME_EVENT stays reserved for a later phase.
    STREAM_STARTED = "stream.started"
    STREAM_ENDED = "stream.ended"
    STREAM_MODE_CHANGED = "stream.mode_changed"
    STREAM_PREFLIGHT_RESULT = "stream.preflight_result"
    STREAM_VIEWER_SEEN = "stream.viewer_seen"
    STREAM_FUNKEN_AWARDED = "stream.funken_awarded"
    STREAM_FUNKEN_CHANGED = "stream.funken_changed"  # nox.stream.funken: every ledger change
    STREAM_MINIGAME_STARTED = "stream.minigame_started"
    STREAM_MINIGAME_ENDED = "stream.minigame_ended"

    OBS_CONNECTED = "obs.connected"
    OBS_DISCONNECTED = "obs.disconnected"
    OBS_SCENE_CHANGED = "obs.scene_changed"
    OBS_HEALTH_CHANGED = "obs.health_changed"
    OBS_CRASH_DETECTED = "obs.crash_detected"
    OBS_AUTO_RESTARTED = "obs.auto_restarted"
    # Not in Spec v0.2 §8's table; additive for the obs plugin (ST-11-02/03) to report OBS's own
    # recording toggle (distinct from the stream output covered by STREAM_STARTED/STREAM_ENDED).
    OBS_RECORDING_CHANGED = "obs.recording_changed"

    TWITCH_CONNECTED = "twitch.connected"
    TWITCH_DISCONNECTED = "twitch.disconnected"
    TWITCH_RESYNCED = "twitch.resynced"
    TWITCH_CHAT_MESSAGE = "twitch.chat_message"
    TWITCH_COMMAND_INVOKED = "twitch.command_invoked"
    TWITCH_EVENT = "twitch.event"
    TWITCH_CHAT_MOOD_CHANGED = "twitch.chat_mood_changed"
    TWITCH_MODERATION_ACTION = "twitch.moderation_action"

    GAME_EVENT = "game.event"

    # Mobile Companion (Spec v0.8 §7, EPIC-17). `remote.message` is what the telegram plugin emits
    # for every inbound message from the paired phone; the rest is core-side lifecycle. No remote
    # event ever carries transcript, memory or secret content (IPC Model "Outbound filtering": the
    # `remote` role never receives `voice.transcript_*`/`ai.*`/`memory.*`).
    REMOTE_MESSAGE = "remote.message"
    REMOTE_PAIRING_STARTED = "remote.pairing_started"
    REMOTE_PAIRED = "remote.paired"
    REMOTE_REVOKED = "remote.revoked"
    REMOTE_COMMAND = "remote.command"
    REMOTE_NOTIFICATION_SENT = "remote.notification_sent"

    # Rocket League Stage 1 (ST-12-xx, Spec v0.3 Rocket League Stage 1). game.detected/game.ended
    # are generic game-lifecycle events (FR-10.10's game-plugin contract); rl.* stays specific to
    # the `rl` plugin.
    GAME_DETECTED = "game.detected"
    GAME_ENDED = "game.ended"
    RL_MATCH_STARTED = "rl.match_started"
    RL_MATCH_ENDED = "rl.match_ended"
    RL_EVENT = "rl.event"
    RL_REPLAY_PARSED = "rl.replay_parsed"
    RL_CALLOUT = "rl.callout"

    # Vision Stage 2 (Spec v0.9 Vision Stage 2, EPIC-18, ST-18-02..06). Additive `rl.vision.*`
    # sub-scope, unchanged observation-only boundary (FR-10.1) - a read-only model pass over frames
    # the `rl` plugin's existing capture pipeline already produces (nox_plugin_rl/vision/*).
    RL_VISION_DETECTIONS = "rl.vision.detections"  # low-rate, confidence-gated (not audited)
    RL_VISION_DISABLED = "rl.vision.disabled"  # budget guard or manual auto/forced disable (P6)
    RL_VISION_ANALYSIS = "rl.vision.analysis"  # post-match rough rotation-position analysis

    # Clip Pipeline (Spec v0.6 §8, EPIC-15). Reduced event surface for this delivery pass: the
    # `clips` plugin never talks to OBS directly (no cross-plugin tool call in the Plugin API), so
    # it only emits CLIP_REQUESTED; the core-side `nox.clips` service calls `obs.replay_buffer.save`
    # through `ToolExecutor` and reports the outcome as CLIP_SAVED/CLIP_FAILED, and CLIP_EXPORTED is
    # emitted by the core `clip.export` tool. Full spec §8 names (clip.captured/created/reviewed/
    # discarded/trim_created/marker_added) are not implemented in this pass - flagged in the report.
    CLIP_REQUESTED = "clip.requested"
    CLIP_SAVED = "clip.saved"
    CLIP_FAILED = "clip.failed"
    CLIP_EXPORTED = "clip.exported"

    # Creative Apps (Spec v0.7 Creative Apps, EPIC-16; pending approval, PO sign-off relayed
    # 2026-09-14). SENSOR_FOREGROUND_CHANGED is the v0.5 process/activity sensor's foreground-
    # window-change event (FR-14.4); defined defensively here because it was not yet present in
    # this file when the `creative` plugin needed it - re-checked absent immediately before this
    # edit. The concurrent v0.5 agent should reuse this definition rather than add a second one.
    SENSOR_FOREGROUND_CHANGED = "sensor.foreground_changed"
    CREATIVE_APP_DETECTED = "creative.app_detected"
    CREATIVE_APP_LEFT = "creative.app_left"
    CREATIVE_NOTE_WRITTEN = "creative.note_written"
    # Not in Spec v0.7 §6's table; additive so the plugin (no privacy-zone visibility) can ask the
    # core-side `nox.creative` service to decide and perform a consent-gated screenshot capture.
    CREATIVE_SCREENSHOT_REQUESTED = "creative.screenshot.requested"
    CREATIVE_SCREENSHOT_RESULT = "creative.screenshot.result"

    # Project Management (Spec v0.4 PM and Coding §8, EPIC-13, ST-13-01 subset). Full spec §8 names
    # (pm.status_changed, pm.dependency_added, pm.board_synced, pm.priority_proposed, ...) are not
    # implemented in this pass - later PM stories, flagged in the report.
    PM_ITEM_CHANGED = "pm.item_changed"
    PM_FOCUS_CHANGED = "pm.focus_changed"

    # PC Awareness Sensors (Spec v0.5 §3.5, EPIC-20, ST-20-04/06/07). PRIVACY_ZONE_CHANGED
    # formalizes the raw string `"privacy.zone_changed"` that `PrivacyService.observe_foreground()`
    # already logs and that `PetService._on_zone` already subscribes to by that same literal -
    # this just gives it a catalog entry and a payload model, it does not rename anything.
    # SENSOR_PROCESS_* is the generic "a configured process name started/ended" signal the
    # `sensors` package emits for any game plugin (e.g. `rl`) to consume instead of duplicating
    # OS process-watching.
    PRIVACY_ZONE_CHANGED = "privacy.zone_changed"
    SENSOR_PROCESS_STARTED = "sensor.process_started"
    SENSOR_PROCESS_ENDED = "sensor.process_ended"

    # Coding Assistant (Spec v0.4 PM and Coding §8, EPIC-14, `plugins/coding`). Narrowed event
    # surface for this delivery pass (see the task brief that added these): the full spec §8 table
    # (coding.session.plan_shown/confirmation_requested/repair_attempt/rolled_back/review_ready/
    # merged/paused/resumed) is not implemented here - `coding.session_progress`'s `stage` field
    # covers plan/implement/test/repairing/review/merge, and repair attempts are visible via
    # `coding.session_progress` (stage=repairing) plus `coding.session_failed`'s `repair_attempts`.
    CODING_SESSION_STARTED = "coding.session_started"
    CODING_SESSION_PROGRESS = "coding.session_progress"
    CODING_SESSION_ENDED = "coding.session_ended"
    CODING_SESSION_FAILED = "coding.session_failed"

    # Personality & Proactivity (Spec v0.5 EPIC-19, ST-19-04/08): every `nox.proactive.notify()`
    # decision is observable, whether it was delivered or held back - the dashboard toast panel and
    # the telegram remote plugin both consume PROACTIVE_NOTIFICATION; PROACTIVE_SUPPRESSED never
    # fires for URGENT security/data-loss cases (those always get through, per B.13).
    PROACTIVE_NOTIFICATION = "proactive.notification"
    PROACTIVE_SUPPRESSED = "proactive.suppressed"

    # Memory & Vault (Spec v0.5 MVP, EPIC-07, ST-07-01/02/03). MEMORY_CREATED/MEMORY_DELETED
    # (memory.created/memory.deleted) were reserved earlier without payload models - this pass adds
    # them plus MEMORY_INDEX_UPDATED for the vault watcher/indexer (ST-07-03) re-indexing a note.
    MEMORY_INDEX_UPDATED = "memory.index_updated"

    # Settings (EPIC-21, `nox.settings`). SETTINGS_CHANGED names the dotted config paths a
    # `config.set` just wrote - paths only, never values: a setting can hold a display name, a
    # channel or a hotkey, and an event payload is the wrong place for any of them. Every UI that
    # caches a setting re-reads it with `config.get` when this arrives.
    SETTINGS_CHANGED = "settings.changed"
    # Twitch OAuth device-code login (`nox.settings.twitch_auth`) moved on: the dashboard's login
    # panel follows this instead of polling `twitch.auth.status`. Carries no token, ever.
    TWITCH_AUTH_CHANGED = "twitch.auth.changed"


# ---- Payload models ----------------------------------------------------------------------------


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


# ---- Stream Bot payload models (Spec v0.2 §8) ---------------------------------------------------


class StreamStarted(BaseModel):
    session_id: str
    mode: str  # live | recording_only
    obs_connected: bool = False
    twitch_connected: bool = False


class StreamEnded(BaseModel):
    session_id: str
    duration_s: float
    summary: str = ""
    ended_reason: str = "manual"  # manual | panic | obs_lost | crash


class StreamModeChanged(BaseModel):
    previous: str  # idle | live | recording_only | paused
    current: str
    by: str = "obs_detected"  # obs_detected | manual


class PreflightItem(BaseModel):
    name: str
    status: str  # green | amber | red
    detail: str = ""


class StreamPreflightResult(BaseModel):
    session_id: str
    items: list[PreflightItem] = Field(default_factory=list)
    overall: str = "amber"  # green | amber | red


class StreamViewerSeen(BaseModel):
    viewer_id: str
    first_time: bool = False


class StreamFunkenAwarded(BaseModel):
    """Spec v0.2 §8: emitted for a viewer earn event (chat activity, sub, bits, raid, ...)."""

    viewer_id: str
    delta: float
    reason: str = ""
    #: Additive (twitch plugin, ST-11-04): plugins never book Funken themselves, they only request
    #: an award; the core's `FunkenService` decides and knows the real balance
    #: (`StreamFunkenChanged` carries the authoritative `balance_after`). Optional here so a
    #: requesting plugin never has to fabricate a number it does not have.
    balance_after: float = 0.0


class StreamFunkenChanged(BaseModel):
    """`nox.stream.funken.FunkenService`: every ledger change (earn/spend/admin/decay), not just
    viewer-activity earns (see `StreamFunkenAwarded`)."""

    viewer_id: str
    delta: float
    reason: str = ""
    balance_after: float
    source: str  # earn | spend | admin | decay
    tier: str = "none"


class StreamMinigameStarted(BaseModel):
    session_id: str
    game_id: str
    participants: list[str] = Field(default_factory=list)


class StreamMinigameEnded(BaseModel):
    session_id: str
    game_id: str
    participants: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)


class ObsConnectionChanged(BaseModel):
    reason: str = ""


class ObsDisconnected(BaseModel):
    reason: str = ""
    backoff_s: float = 0.0


class ObsSceneChanged(BaseModel):
    previous_scene: str = ""
    current_scene: str
    by: str = "nox"  # nox | manual


class ObsHealthChanged(BaseModel):
    mic_level_ok: bool = True
    camera_ok: bool = True
    render_lag_pct: float = 0.0
    dropped_frames_pct: float = 0.0


class ObsCrashDetected(BaseModel):
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ObsAutoRestarted(BaseModel):
    attempt: int
    succeeded: bool


class ObsRecordingChanged(BaseModel):
    """Additive (obs plugin, ST-11-02/03): OBS's own recording toggle via `RecordStateChanged`."""

    recording: bool = False
    by: str = "obs_detected"  # obs_detected | manual


class TwitchConnectionChanged(BaseModel):
    reason: str = ""


class TwitchDisconnected(BaseModel):
    reason: str = ""
    backoff_s: float = 0.0


class TwitchResynced(BaseModel):
    messages_recovered: int = 0
    messages_lost_estimate: int = 0
    gap_s: float = 0.0


class TwitchChatMessage(BaseModel):
    """Every inbound chat message is tagged `channel: "public"` by construction (FR-5.6/FR-9.7):
    it is never eligible for the private voice/dashboard-only channel. The IPC hub additionally
    keeps this event away from the `pet`/`remote` roles (`nox.ipc.server.REDACTED_FROM`)."""

    chat_event_id: int
    viewer_id: str = ""
    text: str
    channel: str = "public"
    #: Additive (twitch plugin, ST-11-04): deterministic `RelevanceClassifier` output - how likely
    #: this message wants a reaction (0-1) and whether it addresses Nox directly (name mention,
    #: command, or a question scored high enough). Defaults keep old payloads valid.
    relevance: float = 0.0
    addressed_to_nox: bool = False


class TwitchCommandInvoked(BaseModel):
    chat_event_id: int
    viewer_id: str = ""
    command: str
    args: list[str] = Field(default_factory=list)


class TwitchEvent(BaseModel):
    chat_event_id: int
    kind: str  # follow | sub | bits | raid | cheer | points_redemption | command
    viewer_id: str = ""
    priority: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


class TwitchChatMoodChanged(BaseModel):
    category: str  # one of eight mood categories (FR-9.14); exact set is pending approval
    window: str = "short"  # short | medium | long


class TwitchModerationAction(BaseModel):
    chat_event_id: int
    viewer_id: str = ""
    stage: str  # ignore | assess | inform | moderate | timeout_confirmed | ban_confirmed
    decision: str = ""
    hard_list_hit: bool = False


class GameDetected(BaseModel):
    """`rl` plugin (ST-12-01, Spec v0.3 §8): a foreground game process was observed. Read-only
    process/window detection - never memory-read or injection."""

    game: str  # e.g. "rocket_league"
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GameEnded(BaseModel):
    game: str
    duration_s: float = 0.0


class RlMatchStarted(BaseModel):
    match_id: int
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RlMatchEnded(BaseModel):
    match_id: int
    duration_s: float = 0.0
    result: str = "unknown"  # win | loss | unknown
    summary_short: str = ""


class RlEvent(BaseModel):
    """Spec v0.3 §8's honest per-kind capability note: `source="hud"` is real-time; `kind="save"`
    is `source="replay"` only in Stage 1 (never emitted from HUD recognition)."""

    match_id: int | None = None
    kind: str  # goal | overtime | boost_low | demo | save | ...
    source: str  # hud | replay
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    payload: dict[str, Any] = Field(default_factory=dict)


class RlReplayParsed(BaseModel):
    # `replay_id` defaults to 0: the plugin worker has no DB access (Plugin API has no SQLite
    # surface), so it reports the file path/parsed header and the core-side persistence service
    # (nox.rl.services.RlPersistenceService) resolves/assigns the real `rl_replays.id`.
    replay_id: int = 0
    parse_status: str = "failed"  # ok | partial | failed
    matched_match_id: int | None = None
    file_path: str = ""
    header: dict[str, Any] = Field(default_factory=dict)
    parser_version: str = ""


class RlCallout(BaseModel):
    """Emitted by the callout engine after a `prepared_clip` plays (transparency/debugging, not
    re-spoken) - carries the measured event-to-audio-start latency (FR-10.3, "measured, not
    assumed")."""

    match_id: int | None = None
    clip_id: str
    rule_id: str
    latency_ms: float = 0.0


class RlVisionDetection(BaseModel):
    """One rough bounding-box detection (fractions of the frame), Spec v0.9 §7."""

    entity: str  # ball | car
    confidence: float = Field(ge=0.0, le=1.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)
    team: str | None = None  # self | opponent | None


class RlVisionDetections(BaseModel):
    """`nox_plugin_rl.vision.FrameSampler` (ST-18-04): already confidence-gated - a detection below
    the configured threshold never reaches this event (silence over guessing, FR-10.3)."""

    match_id: int | None = None
    backend: str = "none"  # none | opencv | onnx
    detections: list[RlVisionDetection] = Field(default_factory=list)


class RlVisionDisabled(BaseModel):
    """Budget guard auto-disable or a manual/forced disable (Spec v0.9 §4.4/§6 - audited, P6
    observable-system)."""

    reason: str
    automatic: bool = True


class RlVisionAnalysis(BaseModel):
    """Post-match rough rotation-position analysis (Spec v0.9 §4.5 boundary note: this is Stage 2's
    live-detection-derived rough signal, never Stage 3's future replay-based precise analysis)."""

    match_id: int | None = None
    frames_analyzed: int = 0
    ball_side_ratio: float | None = None  # fraction of ball detections on the self half (x<0.5)
    avg_self_car_ball_distance: float | None = None  # rough proximity signal, 0..~1.4 (frame diag)
    coaching_summary: str = ""


# ---- Mobile Companion (Spec v0.8 §7, EPIC-17) --------------------------------------------------


class RemoteMessage(BaseModel):
    """One inbound message from the phone, as emitted by the `telegram` plugin. `sender_id` is the
    transport's own verified sender identity (Telegram user id) - the core decides whether it is
    paired, the plugin never does. `update_id` is the transport's monotonic sequence number, used
    for replay rejection."""

    channel: str = "telegram"
    sender_id: str
    chat_id: str = ""
    update_id: int = 0
    text: str = ""


class RemotePairingStarted(BaseModel):
    """A one-time pairing code was issued (dashboard/shell). The code itself is never in a payload
    or an audit entry - only its id and expiry (Spec v0.8 §6)."""

    pairing_id: str
    expires_at: str


class RemotePaired(BaseModel):
    device_id: str
    name: str = ""
    channel: str = "telegram"


class RemoteRevoked(BaseModel):
    device_id: str
    reason: str = ""


class RemoteCommand(BaseModel):
    """Every remote command attempt, allowed or not (Spec v0.8 §5.3). `command` is the verb only;
    arguments and free chat text are never in the payload."""

    channel: str = "telegram"
    sender_id: str
    device_id: str = ""
    command: str
    allowed: bool
    reason: str = ""


class RemoteNotificationSent(BaseModel):
    """An allow-listed event was forwarded to the phone, or suppressed by the quiet-hours/zone
    gate. Carries the event *name* and the gate decision, never the notification text."""

    event: str
    sent: bool
    reason: str = ""


# ---- Clip Pipeline payload models (Spec v0.6 §8, EPIC-15) ----------------------------------------


class ClipRequested(BaseModel):
    """`clips` plugin -> core: a highlight candidate was scored above threshold or a manual trigger
    fired; the core-side service (never the plugin) calls `obs.replay_buffer.save`."""

    trigger_kind: str  # rl.goal | rl.save | chat_hype | user_marker | twitch_command | ...
    source: str  # event | manual
    origin_event_id: str = ""
    session_id: str = ""
    tags: list[str] = Field(default_factory=list)


class ClipSaved(BaseModel):
    """Core: the replay-buffer file was resolved, copied into the clip library and indexed
    (`clips` row, `status="new"`)."""

    clip_id: str
    file_path: str
    trigger_kind: str
    source: str
    duration_s: float = 0.0
    tags: list[str] = Field(default_factory=list)


class ClipFailed(BaseModel):
    """Core or `clips` plugin: a clip could not be captured (replay buffer disabled, OBS
    unreachable, cooldown, watcher timeout, ...). Never a crash - always this event or a
    non-crashing tool-result error instead."""

    trigger_kind: str
    source: str
    reason: str


class ClipExported(BaseModel):
    """`clip.export` tool: the file was copied into `config.clips.export_root`. No network call is
    ever made for this event or its handler."""

    clip_id: str
    export_path: str


class SensorForegroundChanged(BaseModel):
    """v0.5 process/activity sensor (FR-14.4): raw foreground-window change. Defined defensively
    for the `creative` plugin's consumption - see the note on `E.SENSOR_FOREGROUND_CHANGED`."""

    process: str
    title: str = ""
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CreativeAppDetected(BaseModel):
    app: str
    window_title: str = ""


class CreativeAppLeft(BaseModel):
    app: str


class CreativeNoteWritten(BaseModel):
    project: str
    path: str


class CreativeScreenshotRequested(BaseModel):
    corr: str
    app: str
    window_title: str = ""


class CreativeScreenshotResult(BaseModel):
    corr: str
    status: str  # captured | refused | unavailable
    reason: str = ""
    path: str = ""


class PmItemChanged(BaseModel):
    """A Project/Epic/Story vault note was created or its indexed fields changed (watcher reindex,
    or a `pm.story.create`/`pm.story.update_status` tool call). ST-13-01 subset - the full spec §8
    event catalogue (`pm.status_changed` with previous/current/by, `pm.dependency_added`,
    `pm.board_synced`, ...) is a later PM story."""

    id: str
    kind: str  # project | epic | story
    status: str
    epic_id: str | None = None
    project_id: str | None = None


class PmFocusEntry(BaseModel):
    id: str
    kind: str
    title: str
    status: str
    priority: str | None = None
    reason: str = ""


class PrivacyZoneChanged(BaseModel):
    """Payload for `E.PRIVACY_ZONE_CHANGED` (`"privacy.zone_changed"`), published by
    `PrivacyService.observe_foreground()`. `zone` is only ever a zone id (`"banking"`,
    `"discord"`, ...), never the window title/content that triggered the match (FR-14.4)."""

    active: bool
    zone: str | None = None


class SensorProcessStarted(BaseModel):
    process: str
    pid: int = 0


class SensorProcessEnded(BaseModel):
    process: str
    pid: int = 0
    duration_s: float = 0.0


class PmFocusChanged(BaseModel):
    """The day's ranked focus list (`pm.focus.today`) changed - fires only when the ranked id
    sequence itself changes, not on every reindex pass."""

    items: list[PmFocusEntry] = Field(default_factory=list)


class CodingSessionStarted(BaseModel):
    """`coding.session.start` spawned a Claude Code CLI session scoped to `workspace`."""

    session_id: str
    story_id: str = ""
    project_id: str = ""
    workspace: str = ""


class CodingSessionProgress(BaseModel):
    """`stage` is one of plan|implement|test|repairing|review|merge (Spec v0.4 §3.5's staging,
    narrowed for this pass); `tool_name`/`files` are populated when the progress is a Claude Code
    tool call (SP-18: tool name plus the file(s) its `input` touched, when the tool has one)."""

    session_id: str
    stage: str
    detail: str = ""
    tool_name: str = ""
    files: list[str] = Field(default_factory=list)


class CodingSessionEnded(BaseModel):
    """`outcome` is `ended` (finished without error) or `cancelled` (stopped by
    `coding.session.stop` or `security.kill_switch`)."""

    session_id: str
    outcome: str = "ended"
    summary: str = ""
    repair_attempts: int = 0


class CodingSessionFailed(BaseModel):
    """After the repair-attempt limit (Personality v1 B.8) or an unrepairable CLI failure
    (max-turns, not-logged-in): `detail` carries the short review-formatter triage."""

    session_id: str
    reason: str
    repair_attempts: int = 0
    detail: str = ""


class ProactiveNotification(BaseModel):
    """`nox.proactive.notify()` delivered (or attempted) something. `kind` is the `SpeechKind`
    used to gate it (`"urgent"` or `"proactive"`); `priority` is the B.13 URGENT category
    (`security` | `data_loss` | `resources` | `task_result`) for `kind="urgent"`, else a plain
    hint priority (`"low"` | `"normal"` | `"high"`). `channel` names where it actually went."""

    kind: str
    priority: str
    text: str
    channel: str  # "speech" | "toast" | "speech+toast"
    spoken: bool = False
    announced: bool = False  # A.13: a short lead-in was spoken first


class ProactiveSuppressed(BaseModel):
    """A hint was held back entirely (never fires for URGENT security/data-loss, per B.13)."""

    kind: str
    priority: str
    reason: str  # e.g. "quiet_hours", "privacy_zone", "muted", "interruption_budget", "focus_mode"


class MemoryCreated(BaseModel):
    id: int
    type: str
    importance: float = Field(ge=0.0, le=1.0)
    source: str = ""
    vault_path: str | None = None


class MemoryDeleted(BaseModel):
    id: int
    reason: str = ""


class MemoryIndexUpdated(BaseModel):
    path: str
    chunks: int = 0


class SettingsChanged(BaseModel):
    """Which configuration paths a `config.set` wrote. Paths only - never the new values."""

    paths: list[str] = Field(default_factory=list)


class TwitchAuthChanged(BaseModel):
    """`idle` | `pending` | `authorized` | `expired` | `error`, plus the account that approved it
    once one has. No token, refresh token or client id is ever part of this payload."""

    state: str
    login: str = ""


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    E.SYSTEM_HEALTH_CHANGED: HealthChanged,
    E.SYSTEM_MODE_CHANGED: ModeChanged,
    E.STATE_CHANGED: StateChanged,
    E.VOICE_TRANSCRIPT_READY: TranscriptReady,
    E.VOICE_KILL_PHRASE: VoiceKillPhrase,
    E.TTS_STARTED: TtsStarted,
    E.AI_REQUEST_STARTED: AiRequestStarted,
    E.AI_RESPONSE_READY: AiResponseReady,
    E.AI_REQUEST_FAILED: AiRequestFailed,
    E.PET_STATE_CHANGED: PetStateChanged,
    E.PRIVACY_MODE_CHANGED: PrivacyModeChanged,
    E.PRIVACY_CAPTURE_CHANGED: CaptureChanged,
    E.SECURITY_KILL_SWITCH: KillSwitch,
    E.SECURITY_PERMISSION_REQUESTED: PermissionRequested,
    E.SECURITY_PERMISSION_DECIDED: PermissionDecided,
    E.SECURITY_AUDIT: AuditEntry,
    E.PLUGIN_STARTED: PluginLifecycle,
    E.PLUGIN_STOPPED: PluginLifecycle,
    E.PLUGIN_FAILED: PluginLifecycle,
    E.HEALTH_REPORT: HealthReport,
    E.STREAM_STARTED: StreamStarted,
    E.STREAM_ENDED: StreamEnded,
    E.STREAM_MODE_CHANGED: StreamModeChanged,
    E.STREAM_PREFLIGHT_RESULT: StreamPreflightResult,
    E.STREAM_VIEWER_SEEN: StreamViewerSeen,
    E.STREAM_FUNKEN_AWARDED: StreamFunkenAwarded,
    E.STREAM_FUNKEN_CHANGED: StreamFunkenChanged,
    E.STREAM_MINIGAME_STARTED: StreamMinigameStarted,
    E.STREAM_MINIGAME_ENDED: StreamMinigameEnded,
    E.OBS_CONNECTED: ObsConnectionChanged,
    E.OBS_DISCONNECTED: ObsDisconnected,
    E.OBS_SCENE_CHANGED: ObsSceneChanged,
    E.OBS_HEALTH_CHANGED: ObsHealthChanged,
    E.OBS_CRASH_DETECTED: ObsCrashDetected,
    E.OBS_AUTO_RESTARTED: ObsAutoRestarted,
    E.TWITCH_CONNECTED: TwitchConnectionChanged,
    E.TWITCH_DISCONNECTED: TwitchDisconnected,
    E.TWITCH_RESYNCED: TwitchResynced,
    E.TWITCH_CHAT_MESSAGE: TwitchChatMessage,
    E.TWITCH_COMMAND_INVOKED: TwitchCommandInvoked,
    E.TWITCH_EVENT: TwitchEvent,
    E.TWITCH_CHAT_MOOD_CHANGED: TwitchChatMoodChanged,
    E.TWITCH_MODERATION_ACTION: TwitchModerationAction,
    E.GAME_DETECTED: GameDetected,
    E.GAME_ENDED: GameEnded,
    E.RL_MATCH_STARTED: RlMatchStarted,
    E.RL_MATCH_ENDED: RlMatchEnded,
    E.RL_EVENT: RlEvent,
    E.RL_REPLAY_PARSED: RlReplayParsed,
    E.RL_CALLOUT: RlCallout,
    E.REMOTE_MESSAGE: RemoteMessage,
    E.REMOTE_PAIRING_STARTED: RemotePairingStarted,
    E.REMOTE_PAIRED: RemotePaired,
    E.REMOTE_REVOKED: RemoteRevoked,
    E.REMOTE_COMMAND: RemoteCommand,
    E.REMOTE_NOTIFICATION_SENT: RemoteNotificationSent,
    E.CLIP_REQUESTED: ClipRequested,
    E.CLIP_SAVED: ClipSaved,
    E.CLIP_FAILED: ClipFailed,
    E.CLIP_EXPORTED: ClipExported,
    E.SENSOR_FOREGROUND_CHANGED: SensorForegroundChanged,
    E.CREATIVE_APP_DETECTED: CreativeAppDetected,
    E.CREATIVE_APP_LEFT: CreativeAppLeft,
    E.CREATIVE_NOTE_WRITTEN: CreativeNoteWritten,
    E.CREATIVE_SCREENSHOT_REQUESTED: CreativeScreenshotRequested,
    E.CREATIVE_SCREENSHOT_RESULT: CreativeScreenshotResult,
    E.PM_ITEM_CHANGED: PmItemChanged,
    E.PM_FOCUS_CHANGED: PmFocusChanged,
    E.PRIVACY_ZONE_CHANGED: PrivacyZoneChanged,
    E.SENSOR_PROCESS_STARTED: SensorProcessStarted,
    E.SENSOR_PROCESS_ENDED: SensorProcessEnded,
    E.CODING_SESSION_STARTED: CodingSessionStarted,
    E.CODING_SESSION_PROGRESS: CodingSessionProgress,
    E.CODING_SESSION_ENDED: CodingSessionEnded,
    E.CODING_SESSION_FAILED: CodingSessionFailed,
    E.PROACTIVE_NOTIFICATION: ProactiveNotification,
    E.PROACTIVE_SUPPRESSED: ProactiveSuppressed,
    E.MEMORY_CREATED: MemoryCreated,
    E.MEMORY_DELETED: MemoryDeleted,
    E.MEMORY_INDEX_UPDATED: MemoryIndexUpdated,
    E.SETTINGS_CHANGED: SettingsChanged,
    E.TWITCH_AUTH_CHANGED: TwitchAuthChanged,
    E.RL_VISION_DETECTIONS: RlVisionDetections,
    E.RL_VISION_DISABLED: RlVisionDisabled,
    E.RL_VISION_ANALYSIS: RlVisionAnalysis,
}


# ---- Bus contract --------------------------------------------------------------------------------

Handler = Callable[[Event], Awaitable[None] | None]


class EventBus(Protocol):
    """Async in-process pub/sub. Implementations must be safe to call from any task.

    - subscribe(pattern): glob patterns, e.g. "voice.*" or "*". Returns an unsubscribe callable.
    - publish(event): validates payload against PAYLOAD_MODELS when registered, dispatches to
      handlers, never raises into the publisher because a handler failed (handler errors are
      logged and emitted as system events).
    - Priority: security.* and system.* handlers run before others.
    """

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]: ...
    async def publish(self, event: Event) -> None: ...
    async def wait_for(
        self, name: str, *, timeout: float | None = None, corr: str | None = None
    ) -> Event: ...


def validate_payload(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a payload for a catalogued event name. Unknown names pass through unchanged."""
    model = PAYLOAD_MODELS.get(name)
    if model is None:
        return payload
    return model.model_validate(payload).model_dump(mode="json")


async def _noop() -> None:  # keeps asyncio import meaningful for type checkers in stubs
    await asyncio.sleep(0)
