"""A minimal fake Twitch IRC server (`asyncio.start_server`, plain TCP - no TLS) for the twitch
plugin's tests. Speaks just enough of the real protocol to exercise the plugin: PASS/NICK, `CAP
REQ` -> ACK, JOIN -> echoed JOIN, PING/PONG bookkeeping, and scriptable PRIVMSG injection with
tags. Shared by unit tests and `tests/integration/test_twitch_plugin.py`.
"""

from __future__ import annotations

import asyncio


class FakeIrcServer:
    def __init__(self, *, require_auth_failure: bool = False) -> None:
        self.require_auth_failure = require_auth_failure
        self.received_lines: list[str] = []
        self.pongs: list[str] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._nick = ""
        self._joined_channel = ""

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _write(self, writer: asyncio.StreamWriter, line: str) -> None:
        writer.write((line + "\r\n").encode("utf-8"))
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.received_lines.append(line)
                if line.startswith("PASS "):
                    if self.require_auth_failure:
                        await self._write(
                            writer, ":tmi.twitch.tv NOTICE * :Login authentication failed"
                        )
                        return
                elif line.startswith("NICK "):
                    self._nick = line[len("NICK ") :].strip()
                elif line.startswith("CAP REQ"):
                    await self._write(
                        writer,
                        ":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands "
                        "twitch.tv/membership",
                    )
                elif line.startswith("JOIN "):
                    channel = line[len("JOIN ") :].strip()
                    self._joined_channel = channel
                    join_prefix = f"{self._nick}!{self._nick}@{self._nick}.tmi.twitch.tv"
                    await self._write(writer, f":{join_prefix} JOIN {channel}")
                elif line.startswith("PONG"):
                    self.pongs.append(line)
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    async def send_ping(self, token: str = "tmi.twitch.tv") -> None:
        for writer in list(self._writers):
            await self._write(writer, f"PING :{token}")

    async def send_privmsg(
        self,
        *,
        text: str,
        login: str = "viewer1",
        user_id: str = "123",
        display_name: str = "Viewer1",
        mod: bool = False,
        subscriber: bool = False,
        badges: str = "",
        channel: str | None = None,
    ) -> None:
        chan = channel or self._joined_channel.lstrip("#") or "testchannel"
        tags = (
            f"@badges={badges};display-name={display_name};mod={1 if mod else 0};"
            f"subscriber={1 if subscriber else 0};user-id={user_id}"
        )
        line = f"{tags} :{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #{chan} :{text}"
        for writer in list(self._writers):
            await self._write(writer, line)

    async def disconnect_all(self) -> None:
        """Force-close every open connection (drives the client's reconnect path)."""
        for writer in list(self._writers):
            writer.close()

    @property
    def joined_channel(self) -> str:
        return self._joined_channel

    @property
    def connection_count(self) -> int:
        return len(self._writers)
