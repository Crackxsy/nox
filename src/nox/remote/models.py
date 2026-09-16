"""Row and result types for the remote (Mobile Companion) area, Spec v0.8 §6/§7.

Nothing here carries a secret: a pairing code exists only in `PairingStart.code`, which is returned
once to the local caller of `remote.pair.start` (dashboard/shell) and never stored, logged, audited
or sent over the transport.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DeviceRow(BaseModel):
    """A row of `paired_devices`. `public_key` (the hashed device key) is never part of this model -
    the dashboard must not be able to display key material (ST-17-03)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    channel: str = "telegram"
    role: str = "remote"
    paired_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str = ""
    last_update_id: int = 0

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


class PairingStart(BaseModel):
    """Result of `remote.pair.start`. `code` is shown locally (dashboard/shell) exactly once."""

    model_config = ConfigDict(frozen=True)

    pairing_id: str
    code: str
    expires_at: datetime
    ttl_s: float


class PairingResult(BaseModel):
    """Outcome of redeeming a code. `reason` is a stable machine token, not a sentence."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    device_id: str = ""
    name: str = ""
    reason: str = ""  # "" | expired | used | unknown_code | already_paired | max_devices


class RemoteDecision(BaseModel):
    """What the command policy decided for one inbound remote message."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    command: str
    reason: str = ""
    device_id: str = ""
    reply: str = ""
    args: list[str] = Field(default_factory=list)
