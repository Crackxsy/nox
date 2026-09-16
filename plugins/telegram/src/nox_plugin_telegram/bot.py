"""Minimal Telegram Bot API client: long-polling `getUpdates` plus `sendMessage`, and nothing else
(EPIC-17, Spec v0.8). Every request goes through the `PluginApi.http()` client, whose egress guard
is scoped to `api.telegram.org:443` by the manifest (ADR-013) - this module cannot reach any other
host even if it tried.

Secret handling: the bot token is part of the Bot API's *URL path*. It is fetched per request from
`nox.security.secrets` via the plugin API, never cached in an attribute, never put in a log line,
and `_redact` strips it from anything httpx puts into an exception message. `last_error` (which the
health check surfaces) is redacted for the same reason.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

TokenProvider = Callable[[], Awaitable[str | None]]
ClientFactory = Callable[[], httpx.AsyncClient]
OnMessage = Callable[[int, str, str, str], Awaitable[None]]
OnConnected = Callable[[], Awaitable[None]]
OnDisconnected = Callable[[str], Awaitable[None]]

#: What the health check reports when no token has been provisioned yet (ES-04).
NO_TOKEN = "no bot token configured: nox/telegram/bot_token"  # noqa: S105 - a name, not a value


class TelegramApiError(RuntimeError):
    """The Bot API answered, but with `ok: false` or a non-2xx status."""


class TelegramBotClient:
    def __init__(
        self,
        *,
        api_base: str,
        token_provider: TokenProvider,
        client_factory: ClientFactory,
        on_message: OnMessage,
        on_connected: OnConnected | None = None,
        on_disconnected: OnDisconnected | None = None,
        poll_timeout_s: int = 25,
        request_timeout_s: float = 35.0,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        self._client_factory = client_factory
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._poll_timeout_s = poll_timeout_s
        self._request_timeout_s = request_timeout_s
        self._min_backoff_s = min_backoff_s
        self._max_backoff_s = max_backoff_s

        self.connected = False
        self.last_error = ""
        self.backoff_s = min_backoff_s
        #: Highest `update_id` accepted so far; the next poll asks for `offset + 1`, which is how
        #: the Bot API acknowledges updates. A replayed update is therefore never re-delivered by
        #: the transport - and the core rejects a stale `update_id` a second time anyway.
        self.offset = 0
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.connected = False

    # -- requests --------------------------------------------------------------------------------

    def _redact(self, text: str, token: str | None) -> str:
        return text.replace(token, "<token>") if token else text

    async def _call(self, method: str, payload: dict[str, Any], *, timeout: float) -> Any:
        token = await self._token_provider()
        if not token:
            raise TelegramApiError(NO_TOKEN)
        url = f"{self._api_base}/bot{token}/{method}"
        try:
            client = self._client_factory()
            async with client:
                response = await client.post(url, json=payload, timeout=timeout)
                body = response.json()
        except Exception as exc:
            raise TelegramApiError(self._redact(str(exc) or type(exc).__name__, token)) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            description = ""
            if isinstance(body, dict):
                description = str(body.get("description", ""))
            raise TelegramApiError(self._redact(description or "bot api rejected the call", token))
        return body.get("result")

    async def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        result = await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text},
            timeout=self._request_timeout_s,
        )
        return result if isinstance(result, dict) else {}

    async def get_updates(self) -> list[dict[str, Any]]:
        result = await self._call(
            "getUpdates",
            {
                "offset": self.offset + 1 if self.offset else 0,
                "timeout": self._poll_timeout_s,
                # Only plain messages; the plugin has no use for edits, callbacks or channel posts,
                # and asking for less means the Bot API sends less.
                "allowed_updates": ["message"],
            },
            timeout=self._request_timeout_s,
        )
        return [u for u in (result or []) if isinstance(u, dict)]

    # -- polling ---------------------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                updates = await self.get_updates()
            except TelegramApiError as exc:
                await self._fail(str(exc))
                continue
            if not self.connected:
                self.connected = True
                self.last_error = ""
                self.backoff_s = self._min_backoff_s
                if self._on_connected is not None:
                    await self._on_connected()
            for update in updates:
                await self._dispatch(update)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        update_id = int(update.get("update_id", 0))
        # Advance the acknowledgement watermark even for updates we do not forward, so a message
        # type we ignore cannot wedge the poll loop by being redelivered forever.
        self.offset = max(self.offset, update_id)
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from")
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
            return
        sender_id = str(sender.get("id", ""))
        chat_id = str(chat.get("id", ""))
        if not sender_id or not chat_id:
            return
        await self._on_message(update_id, sender_id, chat_id, text)

    async def _fail(self, reason: str) -> None:
        was_connected = self.connected
        self.connected = False
        self.last_error = reason
        if was_connected and self._on_disconnected is not None:
            await self._on_disconnected(reason)
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=self.backoff_s)
        except TimeoutError:
            pass
        self.backoff_s = min(self._max_backoff_s, max(self._min_backoff_s, self.backoff_s * 2))
