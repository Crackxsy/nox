"""Chat behaviour a viewer can see: the command list, `!streamplan`, spam protection, Funken for
chatting / bits / subs / raids, ignoring other bots, and a connection that heals itself (silent
death, `RECONNECT`, a refused login)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from nox_plugin_twitch.plugin import (
    INFO_COMMAND_COOLDOWN_S,
    LISTED_COMMANDS,
    MESSAGE_AWARD_GAP_S,
    command_list,
)
from nox_plugin_twitch.protocol import parse_chat_tags

from .conftest import FakeClient
from .fake_irc_server import FakeIrcServer
from .test_commands import _plugin
from .test_irc_client import _make_client, _wait_until

pytestmark = pytest.mark.timeout(30)


class RawIrcServer(FakeIrcServer):
    """The fake server plus lines the stock one cannot script (USERNOTICE, RECONNECT, bits)."""

    async def send_raw(self, line: str) -> None:
        for writer in list(self._writers):
            await self._write(writer, line)

    async def chat(self, login: str, text: str, *, user_id: str = "123", tags: str = "") -> None:
        extra = f";{tags}" if tags else ""
        await self.send_raw(
            f"@display-name={login};user-id={user_id}{extra} "
            f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #testchannel :{text}"
        )


@pytest.fixture
async def server() -> AsyncIterator[RawIrcServer]:
    srv = RawIrcServer()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _replies(server: FakeIrcServer) -> list[str]:
    return [
        line.removeprefix("PRIVMSG #testchannel :")
        for line in server.received_lines
        if line.startswith("PRIVMSG #testchannel :")
    ]


async def _wait_replies(server: FakeIrcServer, count: int) -> list[str]:
    await _wait_until(lambda: len(_replies(server)) >= count)
    return _replies(server)


def _awards(client: FakeClient) -> list[dict]:
    return [p for name, p in client.events if name == "stream.funken_awarded"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# -- the command list ---------------------------------------------------------------------------


async def test_befehle_lists_every_command(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    try:
        await server.chat("viewer1", "!befehle")
        reply = (await _wait_replies(server, 1))[0]
        assert reply == command_list("de")
        for expected in ("!rps", "!funken", "!streamplan", "!befehle"):
            assert expected in reply
    finally:
        await plugin.stop()


async def test_commands_is_the_english_alias(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    try:
        await server.chat("viewer1", "!COMMANDS")
        reply = (await _wait_replies(server, 1))[0]
        assert reply == command_list("en")
        assert "!schedule" in reply
    finally:
        await plugin.stop()


async def test_every_listed_command_is_handled(server: RawIrcServer) -> None:
    """The list is built from one table; this keeps it honest - nothing is advertised that the
    bot does not answer, in either language."""
    client = FakeClient()
    _api, plugin = await _plugin(server, client)
    try:
        for name, german, english in LISTED_COMMANDS:
            assert name in plugin._commands
            for usage in (german, english):
                assert usage.split()[0].removeprefix("!") in plugin._commands
    finally:
        await plugin.stop()


# -- !streamplan --------------------------------------------------------------------------------


async def test_streamplan_without_a_schedule_says_so(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    try:
        await server.chat("viewer1", "!streamplan")
        reply = (await _wait_replies(server, 1))[0]
        assert reply == "Es ist noch kein Streamplan hinterlegt."
    finally:
        await plugin.stop()


async def test_streamplan_answers_the_configured_text(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    plugin.settings = plugin.settings.model_copy(update={"schedule_text": "Mo, Mi, Fr ab 19 Uhr"})
    try:
        await server.chat("viewer1", "!streamplan")
        await server.chat("viewer2", "!schedule", user_id="456")
        replies = await _wait_replies(server, 2)
        assert "Streamplan: Mo, Mi, Fr ab 19 Uhr" in replies
        assert "Schedule: Mo, Mi, Fr ab 19 Uhr" in replies
    finally:
        await plugin.stop()


# -- spam protection ----------------------------------------------------------------------------


async def test_info_command_answers_once_per_cooldown_for_everyone(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    clock = _Clock()
    plugin._clock = clock
    try:
        await server.chat("viewer1", "!befehle")
        await _wait_replies(server, 1)
        await server.chat("viewer2", "!befehle", user_id="456")
        await server.chat("viewer1", "!hilfe")  # a different command: its own cooldown
        await _wait_replies(server, 2)
        await asyncio.sleep(0.1)
        assert len(_replies(server)) == 2

        clock.now += INFO_COMMAND_COOLDOWN_S + 1
        await server.chat("viewer2", "!befehle", user_id="456")
        assert len(await _wait_replies(server, 3)) == 3
    finally:
        await plugin.stop()


async def test_unknown_commands_get_no_answer(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    try:
        await server.chat("viewer1", "!nichtda")
        await server.chat("viewer1", "!befehle")
        replies = await _wait_replies(server, 1)
        assert replies == [command_list("de")]
    finally:
        await plugin.stop()


# -- Funken for chat activity -------------------------------------------------------------------


async def test_chatting_requests_funken_at_the_configured_rate(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client)
    clock = _Clock()
    plugin._clock = clock
    try:
        await server.chat("viewer1", "hallo zusammen", user_id="42")
        await _wait_until(lambda: len(_awards(client)) == 1)
        assert _awards(client)[0] == {
            "viewer_id": "42",
            "delta": 0.0,
            "reason": "message",
            "units": 1.0,
        }

        await server.chat("viewer1", "noch eine", user_id="42")
        await server.chat("viewer1", "!funken", user_id="42")  # a command is not "chatting"
        await asyncio.sleep(0.15)
        assert len(_awards(client)) == 1

        clock.now += MESSAGE_AWARD_GAP_S + 1
        await server.chat("viewer1", "wieder da", user_id="42")
        await _wait_until(lambda: len(_awards(client)) == 2)
    finally:
        await plugin.stop()


async def test_cheered_bits_request_funken_per_bit(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client)
    try:
        await server.chat("viewer1", "cheer100 gg", user_id="42", tags="bits=100")
        await _wait_until(lambda: any(a["reason"] == "bit" for a in _awards(client)))
        bit = next(a for a in _awards(client) if a["reason"] == "bit")
        assert bit["units"] == 100.0
        assert bit["viewer_id"] == "42"
    finally:
        await plugin.stop()


@pytest.mark.parametrize(
    ("msg_id", "extra", "reason", "units"),
    [
        ("sub", "", "sub", 1.0),
        ("resub", "", "sub", 1.0),
        ("subgift", "", "sub", 1.0),
        ("submysterygift", ";msg-param-mass-gift-count=5", "sub", 5.0),
        ("submysterygift", ";msg-param-mass-gift-count=999999", "sub", 100.0),
        ("raid", ";msg-param-viewerCount=30", "raid", 1.0),
    ],
)
async def test_subs_gifts_and_raids_request_funken(
    server: RawIrcServer, msg_id: str, extra: str, reason: str, units: float
) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client)
    try:
        await server.send_raw(
            f"@msg-id={msg_id};login=viewer1;user-id=42{extra} "
            ":tmi.twitch.tv USERNOTICE #testchannel :hello"
        )
        await _wait_until(lambda: len(_awards(client)) == 1)
        assert _awards(client)[0] == {
            "viewer_id": "42",
            "delta": 0.0,
            "reason": reason,
            "units": units,
        }
    finally:
        await plugin.stop()


async def test_other_notices_earn_nothing(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client)
    try:
        await server.send_raw(
            "@msg-id=ritual;login=viewer1;user-id=42 :tmi.twitch.tv USERNOTICE #testchannel :hi"
        )
        await server.chat("viewer2", "danach", user_id="7")
        await _wait_until(lambda: len(_awards(client)) >= 1)
        assert [a["viewer_id"] for a in _awards(client)] == ["7"]
    finally:
        await plugin.stop()


# -- other bots ---------------------------------------------------------------------------------


async def test_other_bots_are_ignored_completely(server: RawIrcServer) -> None:
    client = FakeClient()
    _api, plugin = await _plugin(server, client, rate_limit_min_gap_s=0.0)
    try:
        await server.chat("Nightbot", "!befehle", user_id="1")
        await server.chat("StreamElements", "hi chat", user_id="2", tags="bits=50")
        await server.chat("viewer1", "!befehle", user_id="3")
        replies = await _wait_replies(server, 1)
        await asyncio.sleep(0.1)

        assert len(replies) == 1  # only the real viewer's command was answered
        seen = {p["viewer_id"] for n, p in client.events if n == "twitch.chat_message"}
        assert seen == {"3"}
        assert all(a["viewer_id"] == "3" for a in _awards(client))
    finally:
        await plugin.stop()


# -- a connection that heals itself -------------------------------------------------------------


def _joins(server: FakeIrcServer) -> int:
    return sum(1 for line in server.received_lines if line.startswith("JOIN "))


async def test_a_silently_dead_connection_is_replaced(server: RawIrcServer) -> None:
    """No FIN, no data: without an idle timeout the bot would look connected and hear nothing."""
    client = _make_client(server, idle_timeout_s=0.3)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await _wait_until(lambda: _joins(server) >= 2, timeout=5.0)
    finally:
        await client.stop()


async def test_pings_keep_a_quiet_connection_alive(server: RawIrcServer) -> None:
    client = _make_client(server, idle_timeout_s=0.5)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        for _ in range(5):
            await asyncio.sleep(0.2)
            await server.send_ping()
        assert _joins(server) == 1
    finally:
        await client.stop()


async def test_reconnect_command_makes_the_client_reconnect(server: RawIrcServer) -> None:
    client = _make_client(server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await server.send_raw(":tmi.twitch.tv RECONNECT")
        await _wait_until(lambda: _joins(server) >= 2)
        await _wait_until(lambda: client.joined)
    finally:
        await client.stop()


async def test_a_malformed_token_is_reported_as_a_login_failure(server: RawIrcServer) -> None:
    client = _make_client(server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        await server.send_raw(":tmi.twitch.tv NOTICE * :Improperly formatted auth")
        await _wait_until(lambda: "Improperly formatted auth" in client.last_error)
    finally:
        await client.stop()


# -- tags ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "bits"),
    [("100", 100), ("0", 0), ("-5", 0), ("lots", 0), ("", 0)],
)
def test_bits_tag_is_a_non_negative_count(raw: str, bits: int) -> None:
    assert parse_chat_tags({"bits": raw}).bits == bits


def test_no_bits_tag_means_no_bits() -> None:
    assert parse_chat_tags({}).bits == 0
