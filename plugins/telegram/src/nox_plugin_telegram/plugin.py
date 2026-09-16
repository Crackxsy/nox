"""Telegram companion plugin (EPIC-17, Spec v0.8 Mobile Companion).

Wraps one `TelegramBotClient` and exposes it as: one inbound event (`remote.message`, one per
message from the phone, carrying the Telegram sender id), two lifecycle events
(`telegram.connected`/`telegram.disconnected`) and two tools (`telegram.send`,
`telegram.status.read`).

Trust boundary: this plugin decides *nothing*. It does not know which sender is paired, it never
looks at a command, and it has no access to transcripts, memory or the kill switch - it listens to
no events at all. `src/nox/remote` is where a message becomes a decision, so the security rules
live below the AI layer and cannot be talked out of a plugin (Security Model §2, ENGINEERING.md).

Health is reported honestly: AVAILABLE only while long polling is actually connected, UNAVAILABLE
with the secret's *name* when no `nox/telegram/bot_token` exists yet (ES-04) - never a fake "ok".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.core.events import HealthStatus
from nox.ipc.errors import ERR_RATE_LIMITED, ERR_UNAVAILABLE, IpcError
from nox.plugins.api import PluginApi
from nox.security.model import Risk

from .bot import NO_TOKEN, TelegramApiError, TelegramBotClient
from .ratelimit import RateLimiter

TELEGRAM_TOKEN_SECRET = "nox/telegram/bot_token"  # noqa: S105 - a secret *name*, not a value


class EmptyInput(BaseModel):
    """Tools that take no arguments."""


class TelegramSendInput(BaseModel):
    """`telegram.send` arguments. Plain text only - there is no field for a file, a photo, a
    forwarded message or raw HTML, so a transcript or a secret cannot be smuggled through a
    structured field."""

    text: str = Field(..., min_length=1, max_length=4096)
    #: Empty = the chat the last inbound message came from (the paired phone's own chat).
    chat_id: str = Field(default="", max_length=64)


class TelegramPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self._max_chars = int(api.config.get("max_message_chars", 3500))
        self._last_chat_id = ""
        self.rate_limiter = RateLimiter(
            max_messages=int(api.config.get("rate_limit_max_messages", 20)),
            window_s=float(api.config.get("rate_limit_window_s", 60.0)),
            min_gap_s=float(api.config.get("rate_limit_min_gap_s", 1.0)),
        )
        self.client = TelegramBotClient(
            api_base=str(api.config.get("api_base", "https://api.telegram.org")),
            token_provider=self._get_token,
            client_factory=lambda: self.api.http(),
            on_message=self._on_message,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            poll_timeout_s=int(api.config.get("poll_timeout_s", 25)),
            request_timeout_s=float(api.config.get("request_timeout_s", 35.0)),
            min_backoff_s=float(api.config.get("min_backoff_s", 1.0)),
            max_backoff_s=float(api.config.get("max_backoff_s", 30.0)),
        )

    async def _get_token(self) -> str | None:
        return await self.api.secrets.get(TELEGRAM_TOKEN_SECRET)

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    async def health(self) -> tuple[HealthStatus, str]:
        if self.client.connected:
            return HealthStatus.AVAILABLE, "polling"
        if await self._get_token() is None:
            return HealthStatus.UNAVAILABLE, NO_TOKEN
        return HealthStatus.UNAVAILABLE, self.client.last_error or "not connected to Telegram"

    # -- inbound ---------------------------------------------------------------------------------

    async def _on_connected(self) -> None:
        await self.api.events.emit("telegram.connected", {"reason": ""})

    async def _on_disconnected(self, reason: str) -> None:
        await self.api.events.emit(
            "telegram.disconnected", {"reason": reason, "backoff_s": self.client.backoff_s}
        )

    async def _on_message(self, update_id: int, sender_id: str, chat_id: str, text: str) -> None:
        """One inbound message -> one `remote.message`. No parsing, no filtering, no reply: the
        core decides whether this sender is paired and what the message means."""
        self._last_chat_id = chat_id
        await self.api.events.emit(
            "remote.message",
            {
                "channel": "telegram",
                "sender_id": sender_id,
                "chat_id": chat_id,
                "update_id": update_id,
                "text": text,
            },
        )

    # -- tools -----------------------------------------------------------------------------------

    async def send(self, data: TelegramSendInput) -> dict[str, Any]:
        """`telegram.send`: low risk, rate-limited, plain text, one recipient chat."""
        allowed, reason = self.rate_limiter.try_acquire()
        if not allowed:
            # `IpcError`, not a bare exception: only a typed error survives the worker's IPC hop
            # with its message intact (see the twitch plugin's `_send` for the same reasoning).
            raise IpcError(ERR_RATE_LIMITED, f"rate limited: {reason}")
        chat_id = data.chat_id or self._last_chat_id
        if not chat_id:
            raise IpcError(ERR_UNAVAILABLE, "no chat to send to yet (no inbound message seen)")
        text = data.text
        if len(text) > self._max_chars:
            text = text[: self._max_chars] + " …"
        try:
            await self.client.send_message(chat_id, text)
        except TelegramApiError as exc:
            raise IpcError(ERR_UNAVAILABLE, f"telegram send failed: {exc}") from exc
        return {"sent": True, "chars": len(text)}

    async def status_read(self, _data: EmptyInput) -> dict[str, Any]:
        """`telegram.status.read`: read-only polling status; never fakes "connected"."""
        return {
            "connected": self.client.connected,
            "offset": self.client.offset,
            "reason": self.client.last_error,
        }


def create(api: PluginApi) -> TelegramPlugin:
    plugin = TelegramPlugin(api)
    api.tools.register(
        "telegram.send",
        TelegramSendInput,
        plugin.send,
        Risk.LOW,
        description="Send a plain-text message to the paired phone's Telegram chat.",
        side_effects=True,
        local=False,
    )
    api.tools.register(
        "telegram.status.read",
        EmptyInput,
        plugin.status_read,
        Risk.READ,
        description="Read the Telegram long-polling connection status.",
        side_effects=False,
        local=False,
    )
    return plugin
