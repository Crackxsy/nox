"""A fake Twitch chat server: the IRC side of `irc.chat.twitch.tv`, on a loopback port.

What it speaks, modelled on Twitch's public IRC documentation: the login (`PASS oauth:...`, `NICK`)
answered by the welcome numerics (001-004, 375/372/376) - or by `NOTICE * :Login authentication
failed` / `Improperly formatted auth` and a closed connection; `CAP REQ` answered by `CAP * ACK`;
`JOIN` answered by the echoed JOIN, the names list (353/366), `ROOMSTATE` and `USERSTATE`; `PING`
answered by `PONG`; `PRIVMSG` from the client judged by Twitch's send rules (a message budget per
window, no identical message twice in a row) and answered by a `msg_ratelimit` / `msg_duplicate`
`NOTICE` when refused. Twitch does not echo a client's own message back to it and neither does
this.

What a test scripts, from the server side: chat lines with the full tag set (`privmsg`), user
notices (subs, gifted subs, raids), notices, `RECONNECT`, raw lines, and failures - connections that
are refused or cut, a server that goes silent (a connection that died without a FIN), a login that
is refused.

The clock for the send rules is injected (`clock`), so a test moves time by assigning to it and
never sleeps. Nothing here is random: user ids come from the login, message ids and timestamps
count up from fixed starts. Every name, channel and text a test or recording uses is invented.
"""

from __future__ import annotations

import asyncio
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

#: Default channel the fake's tests join; it is only a name.
CHANNEL = "examplechannel"
SERVER_HOST = "tmi.twitch.tv"
#: `tmi-sent-ts` counts up from here (milliseconds, 2026-01-01T00:00:00Z).
_TS_START = 1767225600000


def _escape_tag(value: str) -> str:
    """IRCv3 tag value escaping, the inverse of what the client reads back."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\:")
        .replace(" ", "\\s")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def tag_block(tags: Mapping[str, str]) -> str:
    """`@a=1;b=two\\sparts` for a mapping of tags (values escaped)."""
    return "@" + ";".join(f"{key}={_escape_tag(str(value))}" for key, value in tags.items())


def user_id_for(login: str) -> str:
    """A stable numeric id per login, so the same viewer keeps the same id across a test."""
    return str(100000 + zlib.crc32(login.encode()) % 900000)


@dataclass(frozen=True)
class ReplyTo:
    """The message a chat line replies to (Twitch's reply feature)."""

    login: str
    message_id: str = "00000000-0000-4000-8000-0000000000aa"
    text: str = "the earlier message"


@dataclass
class IrcConnection:
    index: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    #: Every line this connection received, in order, without the line break.
    lines: list[str] = field(default_factory=list)
    token: str = ""
    nick: str = ""
    capabilities: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    logged_in: bool = False
    closed: bool = False
    #: A silent connection reads and ignores everything: no PONG, no JOIN echo, nothing.
    silent: bool = False


@dataclass(frozen=True)
class Delivered:
    """A PRIVMSG the server accepted from a client."""

    connection: int
    channel: str
    text: str
    at: float


class FakeTwitchIrc:
    """One fake Twitch chat server. `valid_token=None` accepts any well-formed login."""

    def __init__(
        self,
        *,
        valid_token: str | None = None,
        send_limit: int = 20,
        send_window_s: float = 30.0,
        reject_duplicates: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.valid_token = valid_token
        self.send_limit = send_limit
        self.send_window_s = send_window_s
        self.reject_duplicates = reject_duplicates
        #: Assign a different callable (or advance the one a test owns) to move time.
        self.clock = clock
        self.port = 0
        self.connections: list[IrcConnection] = []
        self.delivered: list[Delivered] = []
        self.refused_for_rate: list[str] = []
        self.refused_as_duplicate: list[str] = []
        self.pongs: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self._refuse = 0
        self._ts = _TS_START
        self._message_counter = 0

    # -- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for connection in list(self.connections):
            await self._close(connection)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> FakeTwitchIrc:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # -- what a test reads --------------------------------------------------------------------

    @property
    def open_connections(self) -> list[IrcConnection]:
        return [c for c in self.connections if not c.closed]

    @property
    def joined(self) -> bool:
        return any(c.channels for c in self.open_connections)

    @property
    def received_lines(self) -> list[str]:
        """Every line any connection received, connection by connection."""
        return [line for c in self.connections for line in c.lines]

    def lines_of(self, command: str) -> list[str]:
        """Received lines whose command is `command` (`PRIVMSG`, `JOIN`, `PASS`, ...)."""
        return [line for line in self.received_lines if line.split(" ", 1)[0] == command]

    # -- what a test scripts ------------------------------------------------------------------

    def refuse_connections(self, count: int = 1) -> None:
        """Accept the next `count` connections and close them at once, before reading a byte."""
        self._refuse = count

    def go_silent(self, connection: IrcConnection | None = None) -> None:
        """Stop answering on a connection that stays open, like a dead path without a FIN."""
        target = connection or self.open_connections[-1]
        target.silent = True

    async def drop_connections(self) -> None:
        """Close every open connection."""
        for connection in list(self.open_connections):
            await self._close(connection)

    async def reconnect(self, *, close: bool = True) -> None:
        """What Twitch does before restarting a chat server: `RECONNECT`, then the line closes.
        With `close=False` the line stays open, so a client that must act on the `RECONNECT` itself
        (and not only on the connection closing) is told apart from one that does not."""
        await self.send_raw(f":{SERVER_HOST} RECONNECT")
        if close:
            await self.drop_connections()

    async def send_raw(self, line: str) -> None:
        """One line to every open connection, as written."""
        for connection in self.open_connections:
            await self._write(connection, line)

    async def send_lines(self, lines: Iterable[str]) -> None:
        """Several raw lines in order (a replayed recording)."""
        for line in lines:
            await self.send_raw(line)

    async def ping(self, token: str = SERVER_HOST) -> None:
        await self.send_raw(f"PING :{token}")

    async def notice(self, text: str, *, msg_id: str = "", channel: str | None = None) -> None:
        tags = f"@msg-id={msg_id} " if msg_id else ""
        target = "*" if channel == "*" else f"#{channel or CHANNEL}"
        await self.send_raw(f"{tags}:{SERVER_HOST} NOTICE {target} :{text}")

    def _next_message_id(self) -> str:
        self._message_counter += 1
        return f"00000000-0000-4000-8000-{self._message_counter:012d}"

    def _next_ts(self) -> str:
        self._ts += 1000
        return str(self._ts)

    async def privmsg(
        self,
        login: str,
        text: str,
        *,
        channel: str | None = None,
        display_name: str | None = None,
        user_id: str | None = None,
        badges: str = "",
        mod: bool = False,
        subscriber: bool = False,
        bits: int = 0,
        emotes: str = "",
        reply_to: ReplyTo | None = None,
        first_msg: bool = False,
        action: bool = False,
        extra_tags: Mapping[str, str] | None = None,
    ) -> str:
        """A chat line with the tags Twitch sends (`badge-info`, `badges`, `color`, `display-name`,
        `emotes`, `first-msg`, `id`, `mod`, `room-id`, `subscriber`, `tmi-sent-ts`, `turbo`,
        `user-id`, `user-type`, and `bits` / `reply-*` when they apply). Returns the line."""
        chan = channel or CHANNEL
        tags: dict[str, str] = {
            "badge-info": "subscriber/3" if subscriber else "",
            "badges": badges or ("subscriber/3" if subscriber else ""),
            "color": "#1E90FF",
            "display-name": display_name or login,
            "emotes": emotes,
            "first-msg": "1" if first_msg else "0",
            "flags": "",
            "id": self._next_message_id(),
            "mod": "1" if mod else "0",
            "returning-chatter": "0",
            "room-id": "424242",
            "subscriber": "1" if subscriber else "0",
            "tmi-sent-ts": self._next_ts(),
            "turbo": "0",
            "user-id": user_id or user_id_for(login),
            "user-type": "mod" if mod else "",
        }
        if bits:
            tags["bits"] = str(bits)
        if reply_to is not None:
            tags.update(
                {
                    "reply-parent-display-name": reply_to.login,
                    "reply-parent-msg-body": reply_to.text,
                    "reply-parent-msg-id": reply_to.message_id,
                    "reply-parent-user-id": user_id_for(reply_to.login),
                    "reply-parent-user-login": reply_to.login,
                }
            )
        if extra_tags:
            tags.update(extra_tags)
        body = f"\x01ACTION {text}\x01" if action else text
        line = f"{tag_block(tags)} :{login}!{login}@{login}.{SERVER_HOST} PRIVMSG #{chan} :{body}"
        await self.send_raw(line)
        return line

    async def usernotice(
        self,
        msg_id: str,
        login: str,
        *,
        text: str = "",
        channel: str | None = None,
        params: Mapping[str, str] | None = None,
        extra_tags: Mapping[str, str] | None = None,
    ) -> str:
        """A `USERNOTICE` (`sub`, `resub`, `subgift`, `submysterygift`, `raid`, `ritual`, ...).
        `params` become `msg-param-<key>` tags, e.g. `{"months": "3"}`."""
        chan = channel or CHANNEL
        tags: dict[str, str] = {
            "badge-info": "",
            "badges": "",
            "color": "#8A2BE2",
            "display-name": login,
            "emotes": "",
            "id": self._next_message_id(),
            "login": login,
            "mod": "0",
            "msg-id": msg_id,
            "room-id": "424242",
            "subscriber": "0",
            "system-msg": f"{login} triggered {msg_id}",
            "tmi-sent-ts": self._next_ts(),
            "user-id": user_id_for(login),
            "user-type": "",
        }
        for key, value in (params or {}).items():
            tags[f"msg-param-{key}"] = value
        if extra_tags:
            tags.update(extra_tags)
        trailing = f" :{text}" if text else ""
        line = f"{tag_block(tags)} :{SERVER_HOST} USERNOTICE #{chan}{trailing}"
        await self.send_raw(line)
        return line

    # -- the connection -----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = IrcConnection(len(self.connections) + 1, reader, writer)
        self.connections.append(connection)
        if self._refuse > 0:
            self._refuse -= 1
            await self._close(connection)
            return
        try:
            while not connection.closed:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                connection.lines.append(line)
                if connection.silent:
                    continue
                await self._on_line(connection, line)
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            connection.closed = True
            writer.close()

    async def _write(self, connection: IrcConnection, line: str) -> None:
        if connection.closed:
            return
        try:
            connection.writer.write((line + "\r\n").encode("utf-8"))
            await connection.writer.drain()
        except ConnectionError:
            connection.closed = True

    async def _close(self, connection: IrcConnection) -> None:
        connection.closed = True
        connection.writer.close()
        try:
            await connection.writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _on_line(self, connection: IrcConnection, line: str) -> None:
        command, _, rest = line.partition(" ")
        if command == "PASS":
            connection.token = rest.strip()
        elif command == "NICK":
            connection.nick = rest.strip().lower()
            await self._login(connection)
        elif command == "CAP":
            await self._cap(connection, rest)
        elif command == "JOIN":
            await self._join(connection, rest.strip())
        elif command == "PING":
            await self._write(connection, f":{SERVER_HOST} PONG {SERVER_HOST} :{rest.lstrip(':')}")
        elif command == "PONG":
            self.pongs.append(line)
        elif command == "PRIVMSG":
            await self._privmsg(connection, rest)
        elif command in ("PART", "QUIT"):
            await self._close(connection)
        else:
            await self._write(
                connection, f":{SERVER_HOST} 421 {connection.nick} {command} :Unknown command"
            )

    async def _login(self, connection: IrcConnection) -> None:
        if not connection.token.startswith("oauth:") or not connection.nick:
            await self._write(connection, f":{SERVER_HOST} NOTICE * :Improperly formatted auth")
            await self._close(connection)
            return
        if self.valid_token is not None and connection.token != f"oauth:{self.valid_token}":
            await self._write(connection, f":{SERVER_HOST} NOTICE * :Login authentication failed")
            await self._close(connection)
            return
        connection.logged_in = True
        nick = connection.nick
        for numeric, text in (
            ("001", ":Welcome, GLHF!"),
            ("002", ":Your host is tmi.twitch.tv"),
            ("003", ":This server is rather new"),
            ("004", ":-"),
            ("375", ":-"),
            ("372", ":You are in a maze of twisty passages."),
            ("376", ":>"),
        ):
            await self._write(connection, f":{SERVER_HOST} {numeric} {nick} {text}")

    async def _cap(self, connection: IrcConnection, rest: str) -> None:
        verb, _, tail = rest.partition(" ")
        if verb != "REQ":
            return
        wanted = tail.lstrip(":").split()
        known = {"twitch.tv/tags", "twitch.tv/commands", "twitch.tv/membership"}
        if all(cap in known for cap in wanted):
            connection.capabilities = wanted
            await self._write(connection, f":{SERVER_HOST} CAP * ACK :{' '.join(wanted)}")
        else:
            await self._write(connection, f":{SERVER_HOST} CAP * NAK :{' '.join(wanted)}")

    async def _join(self, connection: IrcConnection, channel: str) -> None:
        if not connection.logged_in:
            return
        nick = connection.nick
        connection.channels.append(channel)
        await self._write(connection, f":{nick}!{nick}@{nick}.{SERVER_HOST} JOIN {channel}")
        await self._write(connection, f":{nick}.{SERVER_HOST} 353 {nick} = {channel} :{nick}")
        await self._write(
            connection, f":{nick}.{SERVER_HOST} 366 {nick} {channel} :End of /NAMES list"
        )
        if "twitch.tv/commands" in connection.capabilities:
            await self._write(
                connection,
                "@emote-only=0;followers-only=-1;r9k=0;room-id=424242;slow=0;subs-only=0 "
                f":{SERVER_HOST} ROOMSTATE {channel}",
            )
            await self._write(
                connection,
                "@badge-info=;badges=;color=;display-name="
                f"{nick};emote-sets=0;mod=0;subscriber=0;user-type= "
                f":{SERVER_HOST} USERSTATE {channel}",
            )

    async def _privmsg(self, connection: IrcConnection, rest: str) -> None:
        target, _, body = rest.partition(" :")
        now = self.clock()
        recent = [d for d in self.delivered if now - d.at < self.send_window_s]
        if len(recent) >= self.send_limit:
            self.refused_for_rate.append(body)
            await self._write(
                connection,
                f"@msg-id=msg_ratelimit :{SERVER_HOST} NOTICE {target} :"
                "Your message was not sent because you are sending messages too quickly.",
            )
            return
        if self.reject_duplicates and self.delivered and self.delivered[-1].text == body:
            self.refused_as_duplicate.append(body)
            await self._write(
                connection,
                f"@msg-id=msg_duplicate :{SERVER_HOST} NOTICE {target} :"
                "Your message was not sent because it is identical to the previous one you sent.",
            )
            return
        self.delivered.append(Delivered(connection.index, target.lstrip("#"), body, now))
