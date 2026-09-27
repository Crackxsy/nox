"""Payload models for the stream companion: Twitch chat and moderation, OBS, the Funken
viewer currency, minigames and the stream session itself."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


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
    """A viewer earned currency: chat activity, a subscription, bits, a raid."""

    viewer_id: str
    delta: float
    reason: str = ""
    #: A plugin never books currency itself, it only requests
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
    """The recording software's own recording toggle, as its control API reports it."""

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
    """Every inbound chat message is tagged `channel: "public"` by construction:
    it is never eligible for the private voice/dashboard-only channel. The IPC hub additionally
    keeps this event away from the `pet`/`remote` roles (`nox.ipc.server.REDACTED_FROM`)."""

    chat_event_id: int
    viewer_id: str = ""
    text: str
    channel: str = "public"
    #: The relevance classifier's deterministic output - how likely
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
    category: str  # one of the eight mood categories the classifier knows
    window: str = "short"  # short | medium | long


class TwitchModerationAction(BaseModel):
    chat_event_id: int
    viewer_id: str = ""
    stage: str  # ignore | assess | inform | moderate | timeout_confirmed | ban_confirmed
    decision: str = ""
    hard_list_hit: bool = False


class GameDetected(BaseModel):
    """A foreground game process was observed by the `rl` plugin. Read-only
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
    """What each source can honestly claim: `source="hud"` is real-time; `kind="save"`
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
    re-spoken) - carries the measured event-to-audio-start latency ("measured, not
    assumed")."""

    match_id: int | None = None
    clip_id: str
    rule_id: str
    latency_ms: float = 0.0


class RlVisionDetection(BaseModel):
    """One rough bounding-box detection, as fractions of the frame."""

    entity: str  # ball | car
    confidence: float = Field(ge=0.0, le=1.0)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)
    team: str | None = None  # self | opponent | None


class RlVisionDetections(BaseModel):
    """Already confidence-gated by the frame sampler: a detection below the configured
    threshold never reaches this event, because silence beats guessing."""

    match_id: int | None = None
    backend: str = "none"  # none | opencv | onnx
    detections: list[RlVisionDetection] = Field(default_factory=list)


class RlVisionDisabled(BaseModel):
    """The budget guard disabled the analysis, or a person did (audited either way,
    observable-system)."""

    reason: str
    automatic: bool = True


class RlVisionAnalysis(BaseModel):
    """Rough post-match rotation analysis (this is the analysis half's
    live-detection-derived rough signal, never Stage 3's future replay-based precise analysis)."""

    match_id: int | None = None
    frames_analyzed: int = 0
    ball_side_ratio: float | None = None  # fraction of ball detections on the self half (x<0.5)
    avg_self_car_ball_distance: float | None = None  # rough proximity signal, 0..~1.4 (frame diag)
    coaching_summary: str = ""
