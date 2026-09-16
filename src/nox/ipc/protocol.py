"""IPC protocol contract (schema version 1).

Every message between core, shell, workers, plugins, pet renderer and dashboard is one Envelope.
Rules (see vault 02 - Architecture/IPC Model):
- typed: `name` is a dotted identifier from the event/request catalog (nox.core.events).
- versioned: `v` is the schema version; receivers reject unknown major versions.
- identifiable: `id` is a UUID4; `corr` links responses/events to the originating request.
- authenticated: the first message on a connection must be `auth` (kind="request") carrying a token;
  everything before successful auth is rejected and the connection closed.
- validated: payloads are validated by the receiver against the registered pydantic model for
  `name`.
- never free-form LLM text as control: LLM output travels as data inside typed payloads only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Kind(StrEnum):
    EVENT = "event"  # fire-and-forget notification
    REQUEST = "request"  # expects exactly one RESPONSE or ERROR with the same corr
    RESPONSE = "response"
    ERROR = "error"
    STREAM = "stream"  # partial results for a request (TTS chunks, AI tokens); corr = request


Role = Literal["core", "shell", "worker", "plugin", "pet", "dashboard", "supervisor", "remote"]


class Source(BaseModel):
    """Who sent the message. `role` is fixed per connection at auth time and cannot be spoofed."""

    model_config = ConfigDict(frozen=True)
    role: Role
    id: str = Field(min_length=1, max_length=64, description="stable instance id, e.g. worker:stt")


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = Field(default=SCHEMA_VERSION, ge=1)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: Kind
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=96)
    corr: str | None = Field(default=None, description="correlation id (request id) if applicable")
    src: Source
    payload: dict[str, Any] = Field(default_factory=dict)

    def reply(
        self, name: str, payload: dict[str, Any], src: Source, *, kind: Kind = Kind.RESPONSE
    ) -> Envelope:
        return Envelope(kind=kind, name=name, corr=self.id, src=src, payload=payload)


class ErrorPayload(BaseModel):
    # dotted codes per IPC Model: auth.denied, validation.failed, permission.denied, not_found,
    # timeout, unavailable, internal, rate_limited
    code: str = Field(pattern=r"^[a-z_]+(\.[a-z_]+)*$")
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AuthRequest(BaseModel):
    """name = 'ipc.auth' (kind=request). The token is issued by core per start (or per device)."""

    token: str = Field(min_length=16)
    role: Role
    id: str
    client_version: str = ""


class AuthResponse(BaseModel):
    ok: bool
    session_id: str
    core_version: str
    schema_version: int = SCHEMA_VERSION
    reason: str = ""


# Well-known message names that every component must understand.
NAME_AUTH = "ipc.auth"
NAME_PING = "ipc.ping"
NAME_PONG = "ipc.pong"
NAME_ERROR = "ipc.error"
NAME_SUBSCRIBE = "ipc.subscribe"  # payload: {"patterns": ["voice.*", "state.changed"]}


# ---- chat.send stream/response contract (OP-9: fixed shape, no tolerant field guessing) ---------


class ChatStreamFrame(BaseModel):
    """`chat.send` stream frame: one incremental text delta per frame (IPC Model §Requests).

    The last frame for a request carries `done=true` and an empty `delta`.
    """

    delta: str
    done: bool = False


class ChatSendResult(BaseModel):
    """`chat.send` final response payload (IPC Model §Request catalogue)."""

    request_id: str
    text: str
    provider: str
    degraded: bool


# ---- Stream Bot core response contracts (Spec v0.2 §8/§9, EPIC-11, ST-11-09) --------------------


class StreamPluginStatus(BaseModel):
    obs: str = "unknown"  # unknown | connected | disconnected
    twitch: str = "unknown"


class StreamSessionStatus(BaseModel):
    """`stream.session.status {}` response (dashboard/shell): `nox.stream.sessions.
    StreamSessionService.status()`."""

    active: bool
    session_id: str | None = None
    started_at: str | None = None
    scene: str | None = None
    plugins: StreamPluginStatus = Field(default_factory=StreamPluginStatus)


class FunkenTopEntry(BaseModel):
    viewer_id: str
    display_name: str = ""
    balance: float
    tier: str = "none"


class FunkenTop(BaseModel):
    """`stream.funken.top {limit}` response (dashboard/shell leaderboard): `nox.stream.booking.
    FunkenBooking.top()`."""

    viewers: list[FunkenTopEntry] = Field(default_factory=list)


# name -> payload model, checked by the hub for every STREAM frame it sends for that request name.
STREAM_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "chat.send": ChatStreamFrame,
}

# ---- Mobile Companion (Spec v0.8 §7/§8, EPIC-17, ST-17-01/03) ----------------------------------


class RemoteDevice(BaseModel):
    """One row of the dashboard's "Remote" page. No key material is ever part of this model - the
    hashed device key never leaves `paired_devices` (ST-17-03)."""

    id: str
    name: str = ""
    channel: str = "telegram"
    paired_at: str
    last_seen_at: str | None = None
    revoked_at: str | None = None
    revoked_reason: str = ""


class RemotePairCode(BaseModel):
    """`remote.pair.start {name}` response. The code is shown locally, once: it is not stored, not
    logged and never sent over the transport by Nox (Spec v0.8 §5.1, pairing-code interception)."""

    pairing_id: str
    code: str
    expires_at: str
    ttl_s: float


class RemoteDevices(BaseModel):
    """`remote.devices.list {}` response."""

    devices: list[RemoteDevice] = Field(default_factory=list)
    enabled: bool = False


class RemoteUnpairResult(BaseModel):
    """`remote.unpair {device_id}` response; `ok` is false when the device was already revoked."""

    ok: bool
    device_id: str = ""


# ---- Clip Pipeline (Spec v0.6, EPIC-15, ST-15-01/05/06) ----------------------------------------


class ClipRecord(BaseModel):
    """One `clips` row (`nox.clips.repository.ClipRow`), as sent to the dashboard."""

    id: str
    source: str
    trigger_kind: str
    origin_event_id: str = ""
    session_id: str = ""
    file_path: str
    duration_s: float = 0.0
    created_at: str
    thumbnail_path: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: str = "new"
    parent_clip_id: str | None = None
    checksum: str = ""
    notes: str = ""


class ClipListResult(BaseModel):
    """`clip.list {status?, limit?}` response."""

    clips: list[ClipRecord] = Field(default_factory=list)


class ClipTagResult(BaseModel):
    """`clip.tag {clip_id, tags?, notes?}` response."""

    clip: ClipRecord


class ClipExportResult(BaseModel):
    """`clip.export {clip_id}` response. `ok=false` (never a raised error) when the source file is
    missing - `export_path` stays `None` and `reason` carries the honest message (Spec v0.6 §9)."""

    ok: bool
    export_path: str | None = None
    reason: str = ""


class ClipTrimResult(BaseModel):
    """`clip.trim {clip_id, in_s, out_s}` response. `ok=false` when no cutting backend (ffmpeg) is
    available - a clear, non-crashing message instead of a raised error (Spec v0.6 §11)."""

    ok: bool
    clip_id: str | None = None
    reason: str = ""


# ---- Dashboard EPIC-08: health history and effective-config views ------------------------------


class HealthHistoryEntry(BaseModel):
    """One `health_history` row (`nox.data.repos.HealthHistoryRow`), as sent to the dashboard."""

    id: int
    ts: str
    component: str
    status: str
    reason: str = ""


class HealthHistoryResult(BaseModel):
    """`health.history {limit?, component?}` response. `entries` is empty (not an error) when the
    table has no rows yet - the caller codes defensively, never assuming history exists."""

    entries: list[HealthHistoryEntry] = Field(default_factory=list)


class ConfigEffective(BaseModel):
    """`config.effective {}` response: the merged, validated `NoxConfig` the running core actually
    uses, as a read-only JSON tree for the dashboard Settings view. Redacted defensively even
    though `NoxConfig` itself must never carry secrets (ENGINEERING.md: secrets only via keyring) -
    any field whose dotted path matches `token|secret|password|key` (case-insensitive) is replaced
    with `"***"` rather than assuming today's schema stays that way forever."""

    config: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


# ---- Settings (EPIC-21, `nox.settings`) --------------------------------------------------------


class ConfigFieldSchema(BaseModel):
    """One editable setting, derived from the pydantic models in `nox.core.config` - the dashboard
    renders a control from this instead of hard-coding a field list that drifts."""

    path: str
    type: str  # string | int | float | bool | enum | list[str]
    options: list[str] | None = None
    min: float | None = None
    max: float | None = None
    restart_required: bool
    group: str  # identity | voice | privacy | ai | pet | integrations | memory | plugins | remote


class ConfigSnapshot(BaseModel):
    """`config.get {}` response: the current value of every editable path plus its schema."""

    values: dict[str, Any] = Field(default_factory=dict)
    schema_: list[ConfigFieldSchema] = Field(default_factory=list, alias="schema")
    user_config_path: str = ""

    model_config = ConfigDict(populate_by_name=True)


class ConfigSetResult(BaseModel):
    """`config.set {values}` response. `ok` is false when at least one path was rejected; the
    accepted ones still applied - a settings form must not lose four good fields over one bad one.
    `applied` took effect in the running core, `restart_required` was written to `user.yaml` and
    takes effect at the next start."""

    ok: bool
    applied: list[str] = Field(default_factory=list)
    restart_required: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class SecretStatus(BaseModel):
    """Presence of one known secret. There is deliberately no value field, masked or otherwise."""

    name: str
    present: bool
    group: str  # twitch | obs | telegram


class SecretsStatus(BaseModel):
    """`secrets.status {}` response."""

    secrets: list[SecretStatus] = Field(default_factory=list)


class SettingsOk(BaseModel):
    """`secrets.set`/`secrets.delete`/`twitch.auth.disconnect`/`personality.set` acknowledgement."""

    ok: bool = True


class TwitchDeviceCode(BaseModel):
    """`twitch.auth.start {}` response: what the user types at `verification_uri`. The device code
    itself stays in the core - only the short user code is meant to be shown."""

    user_code: str
    verification_uri: str
    expires_in: float
    interval: float


class TwitchAuthStatus(BaseModel):
    """`twitch.auth.status {}` response. Never carries a token, a refresh token or a client id."""

    state: str  # idle | pending | authorized | expired | error
    login: str | None = None
    expires_at: str | None = None
    scopes: list[str] | None = None
    error: str | None = None


class PersonalityText(BaseModel):
    """`personality.get {}` response: the editable character text and where it lives on disk."""

    text: str
    path: str


# name -> payload model, checked by the dispatcher against every handler's RESPONSE payload.
RESPONSE_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "chat.send": ChatSendResult,
    "stream.session.status": StreamSessionStatus,
    "stream.funken.top": FunkenTop,
    "remote.pair.start": RemotePairCode,
    "remote.devices.list": RemoteDevices,
    "remote.unpair": RemoteUnpairResult,
    "clip.list": ClipListResult,
    "clip.tag": ClipTagResult,
    "clip.export": ClipExportResult,
    "clip.trim": ClipTrimResult,
    "health.history": HealthHistoryResult,
    "config.effective": ConfigEffective,
    "config.get": ConfigSnapshot,
    "config.set": ConfigSetResult,
    "secrets.status": SecretsStatus,
    "secrets.set": SettingsOk,
    "secrets.delete": SettingsOk,
    "twitch.auth.start": TwitchDeviceCode,
    "twitch.auth.status": TwitchAuthStatus,
    "twitch.auth.disconnect": SettingsOk,
    "personality.get": PersonalityText,
    "personality.set": SettingsOk,
}
