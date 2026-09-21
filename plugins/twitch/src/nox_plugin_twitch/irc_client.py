"""Twitch chat IRC client (Stream Bot). Plain `asyncio` streams over TLS
(`ssl.create_default_context`) - no extra dependency, matches "no fake implementations" by never
widening egress on its own (the caller's `authorize` hook enforces the manifest-scoped
`EgressGuard` before every connection attempt, same pattern as `nox_plugin_obs.ws_client`).

PASS/NICK, `CAP REQ` (tags, commands, membership), `JOIN`, PING/PONG, and a reconnect-with-backoff
loop identical in shape to `ObsWebSocketClient._run`. `connected`/`joined` are reported honestly:
`joined` only flips once the server echoes back our own JOIN.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable

from nox.core.logging import get_logger
from nox.plugins.reconnect import ReconnectBackoff

from .protocol import ChatTags, parse_chat_tags, parse_line

log = get_logger(__name__)

REQUESTED_CAPS = "twitch.tv/tags twitch.tv/commands twitch.tv/membership"

PrivmsgHandler = Callable[[ChatTags, str, str, str], Awaitable[None] | None]
ConnectedHook = Callable[[], Awaitable[None]]
DisconnectedHook = Callable[[str], Awaitable[None]]
TokenProvider = Callable[[], Awaitable[str | None]]
NickProvider = Callable[[], Awaitable[str | None]]
Connector = Callable[[str, int, bool], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class TwitchCredentialsMissingError(RuntimeError):
    """`nox/twitch/oauth_token` and/or `nox/twitch/bot_username` are not set yet (ES-01: no bot
    account exists at all until the operator creates one)."""


class TwitchAuthError(RuntimeError):
    """Twitch's server sent a NOTICE rejecting our PASS/NICK."""


async def _default_connector(
    host: str, port: int, tls: bool
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    ssl_context = ssl.create_default_context() if tls else None
    return await asyncio.open_connection(host, port, ssl=ssl_context)


class TwitchIrcClient:
    """One Twitch IRC connection, reconnecting with exponential backoff until `stop`."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        channel: str,
        token_provider: TokenProvider,
        nick_provider: NickProvider,
        on_privmsg: PrivmsgHandler,
        on_connected: ConnectedHook | None = None,
        on_disconnected: DisconnectedHook | None = None,
        tls: bool = True,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        connector: Connector | None = None,
        authorize: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.channel = channel.lstrip("#")
        self.tls = tls
        self._token_provider = token_provider
        self._nick_provider = nick_provider
        self._on_privmsg = on_privmsg
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        #: Called synchronously before every connection attempt (: raw asyncio streams
        #: bypass `PluginApi.http()`'s automatic `EgressGuard`, so the plugin enforces it itself).
        self._authorize = authorize
        self._min_backoff = min_backoff_s
        self._max_backoff = max_backoff_s
        self._connector: Connector = connector or _default_connector
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._nick = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.joined = False
        self.last_error = ""
        self.backoff_s = min_backoff_s

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="twitch-irc-client")

    async def stop(self) -> None:
        self._stop.set()
        await self._close_writer()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.connected = False
        self.joined = False

    async def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception as exc:  # noqa: BLE001 - best-effort close during shutdown/reconnect
            log.debug("twitch.irc_close_failed", error=str(exc))

    async def _run(self) -> None:
        backoff = ReconnectBackoff("twitch", min_s=self._min_backoff, max_s=self._max_backoff)
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except Exception as exc:  # noqa: BLE001 - the reconnect loop must never die
                backoff.failed(exc)
            else:
                backoff.succeeded()
            self.last_error = backoff.last_error
            was_connected = self.connected
            self.connected = False
            self.joined = False
            await self._close_writer()
            if was_connected and self._on_disconnected is not None:
                await self._on_disconnected(self.last_error)
            if self._stop.is_set():
                return
            self.backoff_s = backoff.current_s
            if await backoff.sleep(self._stop):
                return

    async def _connect_once(self) -> None:
        if self._authorize is not None:
            self._authorize()
        token = await self._token_provider()
        nick = await self._nick_provider()
        if not token or not nick:
            raise TwitchCredentialsMissingError(
                "nox/twitch/oauth_token and nox/twitch/bot_username must both be set"
            )
        self._nick = nick
        reader, writer = await self._connector(self.host, self.port, self.tls)
        self._reader, self._writer = reader, writer
        pass_token = token if token.startswith("oauth:") else f"oauth:{token}"
        await self._send_raw(f"PASS {pass_token}")
        await self._send_raw(f"NICK {nick}")
        await self._send_raw(f"CAP REQ :{REQUESTED_CAPS}")
        await self._send_raw(f"JOIN #{self.channel}")
        self.connected = True
        self.last_error = ""
        if self._on_connected is not None:
            await self._on_connected()
        while True:
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("connection closed by server")
            await self._dispatch(raw.decode("utf-8", errors="replace"))

    async def _dispatch(self, raw: str) -> None:
        line = raw.rstrip("\r\n")
        if not line:
            return
        msg = parse_line(line)
        if msg.command == "PING":
            target = msg.params[-1] if msg.params else ""
            await self._send_raw(f"PONG :{target}")
            return
        if msg.command == "NOTICE":
            text = " ".join(msg.params).lower()
            if "authentication failed" in text or "login authentication failed" in text:
                raise TwitchAuthError(" ".join(msg.params))
            return
        if msg.command == "JOIN":
            if msg.nick and self._nick and msg.nick.lower() == self._nick.lower():
                self.joined = True
            return
        if msg.command == "PRIVMSG":
            channel = msg.params[0].lstrip("#") if msg.params else ""
            text = msg.params[-1] if msg.params else ""
            tags = parse_chat_tags(msg.tags)
            result = self._on_privmsg(tags, msg.nick, channel, text)
            if result is not None:
                await result

    # -- sending -------------------------------------------------------------------------------

    async def send_privmsg(self, text: str) -> None:
        if self._writer is None or not self.connected:
            raise ConnectionError(f"not connected to Twitch ({self.last_error or 'no session'})")
        await self._send_raw(f"PRIVMSG #{self.channel} :{text}")

    async def _send_raw(self, line: str) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("not connected")
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()


__all__ = [
    "TwitchAuthError",
    "TwitchCredentialsMissingError",
    "TwitchIrcClient",
]
