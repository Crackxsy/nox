"""`TwitchIrcClient` against the fake IRC server: login/join, PING/PONG, tag parsing on inbound
PRIVMSG, and reconnect with backoff after the connection drops."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_twitch.irc_client import TwitchIrcClient
from nox_plugin_twitch.protocol import ChatTags

from .fake_irc_server import FakeIrcServer

pytestmark = pytest.mark.timeout(30)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


def _make_client(
    irc_server: FakeIrcServer,
    *,
    on_privmsg=None,
    token_provider=None,
    nick_provider=None,
    **overrides,
) -> TwitchIrcClient:
    async def _token() -> str:
        return "s3cret"

    async def _nick() -> str:
        return "noxbot"

    async def _default_on_privmsg(_tags, _login, _channel, _text):
        return None

    return TwitchIrcClient(
        "127.0.0.1",
        irc_server.port,
        channel="testchannel",
        token_provider=token_provider or _token,
        nick_provider=nick_provider or _nick,
        on_privmsg=on_privmsg or _default_on_privmsg,
        tls=False,
        min_backoff_s=0.05,
        max_backoff_s=0.2,
        **overrides,
    )


async def test_login_and_join(irc_server: FakeIrcServer) -> None:
    client = _make_client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        assert any(line.startswith("PASS oauth:s3cret") for line in irc_server.received_lines)
        assert any(line == "NICK noxbot" for line in irc_server.received_lines)
        assert any(line.startswith("CAP REQ") for line in irc_server.received_lines)
        assert irc_server.joined_channel == "#testchannel"
    finally:
        await client.stop()


async def test_ping_is_answered_with_pong(irc_server: FakeIrcServer) -> None:
    client = _make_client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await irc_server.send_ping("tmi.twitch.tv")
        await _wait_until(lambda: len(irc_server.pongs) > 0)
        assert irc_server.pongs[-1] == "PONG :tmi.twitch.tv"
    finally:
        await client.stop()


async def test_privmsg_tags_are_parsed_and_forwarded(irc_server: FakeIrcServer) -> None:
    received: list[tuple[ChatTags, str, str, str]] = []

    async def on_privmsg(tags, login, channel, text):
        received.append((tags, login, channel, text))

    client = _make_client(irc_server, on_privmsg=on_privmsg)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await irc_server.send_privmsg(
            text="Hello chat",
            login="viewer1",
            user_id="42",
            display_name="Viewer1",
            mod=True,
            subscriber=True,
            badges="moderator/1,premium/1",
        )
        await _wait_until(lambda: len(received) > 0)
        tags, login, channel, text = received[0]
        assert login == "viewer1"
        assert channel == "testchannel"
        assert text == "Hello chat"
        assert tags.display_name == "Viewer1"
        assert tags.user_id == "42"
        assert tags.mod is True
        assert tags.subscriber is True
        assert tags.badges == ["moderator/1", "premium/1"]
    finally:
        await client.stop()


async def test_send_privmsg_writes_to_the_server(irc_server: FakeIrcServer) -> None:
    client = _make_client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await client.send_privmsg("gg everyone")
        await _wait_until(
            lambda: any(
                "PRIVMSG #testchannel :gg everyone" in line for line in irc_server.received_lines
            )
        )
    finally:
        await client.stop()


async def test_reconnects_with_backoff_after_disconnect(irc_server: FakeIrcServer) -> None:
    client = _make_client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await irc_server.disconnect_all()
        await _wait_until(lambda: not client.connected)
        await _wait_until(lambda: client.joined, timeout=5.0)  # reconnected and rejoined
        assert irc_server.received_lines.count("NICK noxbot") >= 2
    finally:
        await client.stop()


async def test_health_is_unavailable_without_credentials(irc_server: FakeIrcServer) -> None:
    async def _no_token() -> None:
        return None

    client = _make_client(irc_server, token_provider=_no_token)
    client.start()
    try:
        await asyncio.sleep(0.2)
        assert client.connected is False
        assert client.joined is False
    finally:
        await client.stop()
