"""The fake Twitch chat server against what Twitch's IRC documentation says, spoken to by a raw
client that shares no code with Nox's. If these fail, the fake is wrong - and every contract test
built on it with it."""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.fakes import wait_until
from tests.workshop.fake_twitch import CHANNEL, FakeTwitchIrc, ReplyTo, tag_block, user_id_for
from tests.workshop.raw_clients import RawIrcClient

pytestmark = pytest.mark.timeout(30)


def _tags(line: str) -> dict[str, str]:
    """The tags of a line, unescaped the IRCv3 way (only what the tests here use)."""
    assert line.startswith("@")
    out = {}
    for pair in line[1:].split(" ", 1)[0].split(";"):
        key, _, value = pair.partition("=")
        out[key] = (
            value.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r").replace("\\n", "\n")
        )
    return out


# -- the login ----------------------------------------------------------------------------------


async def test_a_login_gets_the_welcome_numerics_the_ack_the_join_and_the_room_state(
    irc: FakeTwitchIrc,
) -> None:
    async with RawIrcClient(irc.port) as client:
        lines = await client.login()
    numerics = [line.split(" ")[1] for line in lines if line.startswith(":tmi.twitch.tv ")]
    assert numerics[:7] == ["001", "002", "003", "004", "375", "372", "376"]
    assert (
        ":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands twitch.tv/membership" in lines
    )
    assert f":noxbot!noxbot@noxbot.tmi.twitch.tv JOIN #{CHANNEL}" in lines
    assert lines[-1].endswith("366 noxbot #examplechannel :End of /NAMES list")
    assert irc.joined


async def test_room_state_and_user_state_follow_the_join_when_commands_were_requested(
    irc: FakeTwitchIrc,
) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        rest = [await client.read_line(), await client.read_line()]
    assert rest[0].endswith(f"ROOMSTATE #{CHANNEL}")
    assert "room-id=424242" in rest[0]
    assert rest[1].endswith(f"USERSTATE #{CHANNEL}")
    assert "display-name=noxbot" in rest[1]


async def test_no_room_state_without_the_commands_capability(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.send("PASS oauth:test-only")
        await client.send("NICK noxbot")
        await client.send("CAP REQ :twitch.tv/tags")
        await client.send(f"JOIN #{CHANNEL}")
        lines = await client.read_until(lambda line: " 366 " in line)
        await client.send("PING :marker")
        pong = await client.read_line()
    assert not any("ROOMSTATE" in line or "USERSTATE" in line for line in lines)
    assert pong.endswith("PONG tmi.twitch.tv :marker")


async def test_an_unknown_capability_is_refused_with_nak(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.send("CAP REQ :twitch.tv/tags example.invalid/nothing")
        assert await client.read_line() == (
            ":tmi.twitch.tv CAP * NAK :twitch.tv/tags example.invalid/nothing"
        )


async def test_a_wrong_token_is_refused_and_the_connection_closed() -> None:
    async with FakeTwitchIrc(valid_token="right") as irc, RawIrcClient(irc.port) as client:
        await client.send("PASS oauth:wrong")
        await client.send("NICK noxbot")
        assert await client.read_to_eof() == [
            ":tmi.twitch.tv NOTICE * :Login authentication failed"
        ]
    assert irc.open_connections == []


async def test_the_right_token_is_accepted() -> None:
    async with FakeTwitchIrc(valid_token="right") as irc, RawIrcClient(irc.port) as client:
        lines = await client.login(token="oauth:right")
    assert any(" 001 " in line for line in lines)


@pytest.mark.parametrize("token", ["no-prefix", ""])
async def test_a_token_without_the_oauth_prefix_is_improperly_formatted(
    irc: FakeTwitchIrc, token: str
) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.send(f"PASS {token}")
        await client.send("NICK noxbot")
        assert await client.read_to_eof() == [":tmi.twitch.tv NOTICE * :Improperly formatted auth"]


async def test_a_join_before_the_login_is_ignored(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.send(f"JOIN #{CHANNEL}")
        await client.send("PING :marker")
        assert (await client.read_line()).endswith("PONG tmi.twitch.tv :marker")
    assert not irc.joined


async def test_an_unknown_command_gets_a_421(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await client.read_line()  # ROOMSTATE
        await client.read_line()  # USERSTATE
        await client.send("FROBNICATE now")
        assert await client.read_line() == ":tmi.twitch.tv 421 noxbot FROBNICATE :Unknown command"


async def test_the_server_records_what_every_connection_sent(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
    assert irc.lines_of("PASS") == ["PASS oauth:test-only"]
    assert irc.lines_of("NICK") == ["NICK noxbot"]
    assert irc.lines_of("JOIN") == [f"JOIN #{CHANNEL}"]


# -- what the server sends ------------------------------------------------------------------------


async def test_ping_is_answered_and_a_pong_is_recorded(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.ping("check")
        lines = await client.read_until(lambda line: line.startswith("PING"))
        await client.send("PONG :check")
        await wait_until(lambda: irc.pongs == ["PONG :check"], what="the PONG")
    assert lines[-1] == "PING :check"


async def test_a_chat_line_carries_the_tags_twitch_sends(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.privmsg("viewer_01", "hallo zusammen", subscriber=True, bits=50)
        line = await client.read_until(lambda line: "PRIVMSG" in line)
    tags = _tags(line[-1])
    for key in (
        "badge-info",
        "badges",
        "color",
        "display-name",
        "emotes",
        "first-msg",
        "id",
        "mod",
        "room-id",
        "subscriber",
        "tmi-sent-ts",
        "turbo",
        "user-id",
        "user-type",
    ):
        assert key in tags, key
    assert tags["user-id"] == user_id_for("viewer_01")
    assert (tags["subscriber"], tags["bits"]) == ("1", "50")
    assert line[-1].endswith(
        f":viewer_01!viewer_01@viewer_01.tmi.twitch.tv PRIVMSG #{CHANNEL} :hallo zusammen"
    )


async def test_message_ids_and_timestamps_count_up_and_are_the_same_every_run(
    irc: FakeTwitchIrc,
) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.privmsg("viewer_01", "eins")
        await irc.privmsg("viewer_02", "zwei")
        first = _tags((await client.read_until(lambda line: "eins" in line))[-1])
        second = _tags(await client.read_line())
    assert first["id"] == "00000000-0000-4000-8000-000000000001"
    assert second["id"] == "00000000-0000-4000-8000-000000000002"
    assert int(second["tmi-sent-ts"]) - int(first["tmi-sent-ts"]) == 1000


async def test_a_reply_carries_the_reply_parent_tags(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.privmsg("viewer_01", "genau", reply_to=ReplyTo("noxbot", text="wie geht's?"))
        tags = _tags((await client.read_until(lambda line: "genau" in line))[-1])
    assert tags["reply-parent-user-login"] == "noxbot"
    assert tags["reply-parent-msg-body"] == "wie geht's?"
    assert tags["reply-parent-user-id"] == user_id_for("noxbot")


async def test_an_action_is_wrapped_in_ctcp_markers(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.privmsg("viewer_01", "winkt", action=True)
        line = (await client.read_until(lambda line: "PRIVMSG" in line))[-1]
    assert line.endswith(":\x01ACTION winkt\x01")


async def test_a_user_notice_carries_its_message_parameters_as_tags(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.usernotice(
            "raid", "raider_01", params={"viewerCount": "24", "login": "raider_01"}
        )
        line = (await client.read_until(lambda line: "USERNOTICE" in line))[-1]
    tags = _tags(line)
    assert tags["msg-id"] == "raid"
    assert tags["msg-param-viewerCount"] == "24"
    assert tags["login"] == "raider_01"
    assert line.endswith(f":tmi.twitch.tv USERNOTICE #{CHANNEL}")


def test_tag_values_are_escaped_the_ircv3_way() -> None:
    assert tag_block({"a": "x y;z\\w", "b": ""}) == "@a=x\\sy\\:z\\\\w;b="


async def test_reconnect_without_close_leaves_the_connection_open(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.reconnect(close=False)
        lines = await client.read_until(lambda line: line.endswith("RECONNECT"))
        await client.send("PING :still-open")
        assert (await client.read_line()).endswith("PONG tmi.twitch.tv :still-open")
    assert lines[-1] == ":tmi.twitch.tv RECONNECT"


async def test_reconnect_says_so_and_then_closes_the_connection(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await irc.reconnect()
        lines = await client.read_to_eof()
    assert ":tmi.twitch.tv RECONNECT" in lines


# -- Twitch's send rules --------------------------------------------------------------------------


async def test_messages_over_the_budget_in_a_window_are_refused_with_msg_ratelimit() -> None:
    now = [1000.0]
    async with (
        FakeTwitchIrc(send_limit=3, send_window_s=30.0, clock=lambda: now[0]) as irc,
        RawIrcClient(irc.port) as client,
    ):
        await client.login()
        for number in range(3):
            await client.send(f"PRIVMSG #{CHANNEL} :nachricht {number}")
        await client.send(f"PRIVMSG #{CHANNEL} :eine zu viel")
        notice = await client.read_until(lambda line: "msg_ratelimit" in line)
        assert [d.text for d in irc.delivered] == ["nachricht 0", "nachricht 1", "nachricht 2"]
        assert irc.refused_for_rate == ["eine zu viel"]
        assert "sending messages too quickly" in notice[-1]

        now[0] += 30.0  # the window has passed
        await client.send(f"PRIVMSG #{CHANNEL} :wieder frei")
        await wait_until(lambda: len(irc.delivered) == 4, what="the message after the window")
    assert irc.delivered[-1].text == "wieder frei"


async def test_the_budget_is_exact_at_the_window_edge() -> None:
    now = [0.0]
    async with (
        FakeTwitchIrc(send_limit=1, send_window_s=30.0, clock=lambda: now[0]) as irc,
        RawIrcClient(irc.port) as client,
    ):
        await client.login()
        await client.send(f"PRIVMSG #{CHANNEL} :a")
        await wait_until(lambda: len(irc.delivered) == 1, what="the first message")
        now[0] = 29.9
        await client.send(f"PRIVMSG #{CHANNEL} :b")
        await client.read_until(lambda line: "msg_ratelimit" in line)
        now[0] = 30.0
        await client.send(f"PRIVMSG #{CHANNEL} :c")
        await wait_until(lambda: len(irc.delivered) == 2, what="the message at exactly 30 s")
    assert [d.text for d in irc.delivered] == ["a", "c"]


async def test_an_identical_message_twice_in_a_row_is_refused_with_msg_duplicate(
    irc: FakeTwitchIrc,
) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await client.send(f"PRIVMSG #{CHANNEL} :gleich")
        await client.send(f"PRIVMSG #{CHANNEL} :gleich")
        await client.read_until(lambda line: "msg_duplicate" in line)
        await client.send(f"PRIVMSG #{CHANNEL} :anders")
        await client.send(f"PRIVMSG #{CHANNEL} :gleich")  # not a duplicate of the previous one
        await wait_until(lambda: len(irc.delivered) == 3, what="the third delivered message")
    assert [d.text for d in irc.delivered] == ["gleich", "anders", "gleich"]
    assert irc.refused_as_duplicate == ["gleich"]


async def test_duplicates_can_be_switched_off() -> None:
    async with FakeTwitchIrc(reject_duplicates=False) as irc, RawIrcClient(irc.port) as client:
        await client.login()
        await client.send(f"PRIVMSG #{CHANNEL} :gleich")
        await client.send(f"PRIVMSG #{CHANNEL} :gleich")
        await wait_until(lambda: len(irc.delivered) == 2, what="both messages")
    assert irc.refused_as_duplicate == []


# -- the failures a test can script ---------------------------------------------------------------


async def test_refuse_connections_closes_the_next_ones_at_once_then_recovers(
    irc: FakeTwitchIrc,
) -> None:
    irc.refuse_connections(2)
    for _ in range(2):
        async with RawIrcClient(irc.port) as client:
            assert await client.read_to_eof() == []
    async with RawIrcClient(irc.port) as client:
        lines = await client.login()
    assert any(" 001 " in line for line in lines)
    assert len(irc.connections) == 3


async def test_a_silent_connection_swallows_everything_and_answers_nothing(
    irc: FakeTwitchIrc,
) -> None:
    async with RawIrcClient(irc.port) as client:
        await client.login()
        await client.read_line()
        await client.read_line()
        irc.go_silent()
        await client.send("PING :nobody-home")
        await wait_until(
            lambda: "PING :nobody-home" in irc.connections[0].lines, what="the server reading it"
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.reader.readline(), 0.3)
    assert irc.open_connections == [] or irc.open_connections[0].silent


async def test_drop_connections_closes_every_open_connection(irc: FakeTwitchIrc) -> None:
    async with RawIrcClient(irc.port) as one, RawIrcClient(irc.port) as two:
        await one.login()
        await two.login("another_bot")
        await irc.drop_connections()
        assert await one.read_to_eof() is not None
        assert await two.read_to_eof() is not None
    assert irc.open_connections == []
