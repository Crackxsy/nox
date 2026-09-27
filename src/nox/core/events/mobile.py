"""Payload models for the mobile companion: pairing, remote commands and notifications."""

from __future__ import annotations

from pydantic import BaseModel


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
    or an audit entry - only its id and expiry."""

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
    """Every remote command attempt, allowed or not. `command` is the verb only;
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
