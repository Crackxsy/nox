"""Payload models for what happens around the desk: clips, the local sensors, the creative
and project assistants, coding sessions, proactive notifications, memory and settings.

The section this came from was headed as the clip pipeline, which described its first
four models and none of the twenty-three after them."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


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
    """The activity sensor's raw foreground-window change. Defined here
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
    or a `pm.story.create`/`pm.story.update_status` tool call). The wider set of names
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
    `"discord"`, ...), never the window title or content that triggered the match."""

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
    """`stage` is one of plan, implement, test, repairing, review, merge.

    `tool_name` and `files` are filled in when the progress report is a tool call: the tool's name
    plus the files its input touched, when it has any.
    """

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


class ViewShown(BaseModel):
    """Nox drew something. What it is, not what is in it - the page fetches the view itself."""

    id: str
    kind: str
    title: str
