"""Twitch chat IRC client (Stream Bot). Plain `asyncio` streams over TLS
(`ssl.create_default_context`) - no extra dependency, matches "no fake implementations" by never
widening egress on its own (the caller's `authorize` hook enforces the manifest-scoped
`EgressGuard` before every connection attempt, same pattern as `nox_plugin_obs.ws_client`).

PASS/NICK, `CAP REQ` (tags, commands, membership), `JOIN`, PING/PONG, and a reconnect-with-backoff
loop identical in shape to `ObsWebSocketClient._run`. `connected`/`joined` are reported honestly:
`joined` only flips once the server echoes back our own JOIN.

Every outgoing line is built from data (chat text, channel, nick, token), so none of them may carry
a line break or NUL: one CR/LF pair inside a chat message would let the rest of it run as a second
IRC command (CRLF injection). `send_privmsg` refuses such text, the channel is checked when the
client is built, PASS/NICK when they are read, and `_send_raw` refuses any such line as a last line
of defence.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from collections.abc import Awaitable, Callable

from nox.core.logging import get_logger
from nox.plugins.reconnect import ReconnectBackoff

from .protocol import ChatTags, parse_chat_tags, parse_line

log = get_logger(__name__)

REQUESTED_CAPS = "twitch.tv/tags twitch.tv/commands twitch.tv/membership"

#: Characters that end or corrupt an IRC line; never allowed inside data we put on the wire.
_LINE_BREAKING = re.compile(r"[\r\n\x00]")
#: Also not allowed in a single IRC parameter (channel, nick, token): a space or comma would split
#: it into several (`JOIN #a,#b`), a leading `:` would turn it into the trailing parameter.
_PARAM_BREAKING = re.compile(r"[\r\n\x00 ,]")


def has_line_break(text: str) -> bool:
    """Whether `text` contains CR, LF or NUL - anything that would end an IRC line early."""
    return _LINE_BREAKING.search(text) is not None


def _require_param(value: str, what: str) -> str:
    if not value or _PARAM_BREAKING.search(value) is not None or value.startswith(":"):
        raise IrcInjectionError(f"{what} contains characters an IRC parameter may not have")
    return value


PrivmsgHandler = Callable[[ChatTags, str, str, str], Awaitable[None] | None]
ConnectedHook = Callable[[], Awaitable[None]]
DisconnectedHook = Callable[[str], Awaitable[None]]
TokenProvider = Callable[[], Awaitable[str | None]]
NickProvider = Callable[[], Awaitable[str | None]]
Connector = Callable[[str, int, bool], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class TwitchCredentialsMissingError(RuntimeError):
    """`nox/twitch/oauth_token` and/or `nox/twitch/bot_username` are not set yet (no bot
    account exists at all until the operator creates one)."""


class TwitchAuthError(RuntimeError):
    """Twitch's server sent a NOTICE rejecting our PASS/NICK."""


class IrcInjectionError(ValueError):
    """Data meant for one IRC line or parameter contains CR/LF/NUL (or, for a parameter, a space
    or comma). Nothing of it is sent."""


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
        if self.channel:
            _require_param(self.channel, "channel")
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
        _require_param(token, "oauth token")
        _require_param(nick, "bot username")
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
        if has_line_break(text):
            raise IrcInjectionError("chat text contains a line break or NUL; not sent")
        if self._writer is None or not self.connected:
            raise ConnectionError(f"not connected to Twitch ({self.last_error or 'no session'})")
        await self._send_raw(f"PRIVMSG #{self.channel} :{text}")

    async def _send_raw(self, line: str) -> None:
        # Last line of defence: whatever built `line`, it goes out as exactly one IRC line or not
        # at all.
        if has_line_break(line):
            raise IrcInjectionError("refusing to send an IRC line containing CR, LF or NUL")
        writer = self._writer
        if writer is None:
            raise ConnectionError("not connected")
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()


__all__ = [
    "IrcInjectionError",
    "TwitchAuthError",
    "TwitchCredentialsMissingError",
    "TwitchIrcClient",
]
