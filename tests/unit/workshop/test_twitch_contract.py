"""Contract tests: Nox's Twitch chat client (`nox_plugin_twitch.irc_client`) and the Twitch plugin
built on it, against the workshop's fake Twitch chat server - an implementation of the IRC side
written without them.

Each test names a promise: what Nox sends to log in and join, what it makes of the lines Twitch
sends (tags, notices, `RECONNECT`), what it does when Twitch does not behave (a refused login,
refused connections, a connection that goes quiet or is cut), and how its own send rules meet
Twitch's. The fake is checked against Twitch's documentation in `test_fake_twitch.py`; here the
thing under test is Nox. Time is the fake's injected clock wherever a rule depends on it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from nox_plugin_twitch import create
from nox_plugin_twitch.irc_client import TwitchIrcClient
from nox_plugin_twitch.plugin import TwitchPlugin, command_list
from nox_plugin_twitch.protocol import ChatTags

from nox.core.events import HealthStatus
from nox.ipc.errors import IpcError
from tests.unit.fakes import wait_until
from tests.unit.plugins.twitch.conftest import FakeClient, RecordingLogger, make_api
from tests.workshop.fake_twitch import CHANNEL, FakeTwitchIrc, ReplyTo, user_id_for

pytestmark = pytest.mark.timeout(30)

MIN_BACKOFF_S = 0.05
MAX_BACKOFF_S = 0.2


# -- the client ---------------------------------------------------------------------------------


class Heard:
    """A client plus what its handlers were given."""

    def __init__(self) -> None:
        self.privmsgs: list[tuple[ChatTags, str, str, str]] = []
        self.notices: list[tuple[dict[str, str], str, str, str]] = []
        self.connected = 0
        self.disconnected: list[str] = []
        self.client: TwitchIrcClient


def _client(
    irc: FakeTwitchIrc,
    *,
    token: str | None = "test-only",
    nick: str | None = "noxbot",
    **overrides: Any,
) -> Heard:
    heard = Heard()

    async def token_provider() -> str | None:
        return token

    async def nick_provider() -> str | None:
        return nick

    async def on_privmsg(tags: ChatTags, login: str, channel: str, text: str) -> None:
        heard.privmsgs.append((tags, login, channel, text))

    async def on_usernotice(tags: dict[str, str], login: str, channel: str, text: str) -> None:
        heard.notices.append((tags, login, channel, text))

    async def on_connected() -> None:
        heard.connected += 1

    async def on_disconnected(reason: str) -> None:
        heard.disconnected.append(reason)

    options: dict[str, Any] = {
        "tls": False,
        "min_backoff_s": MIN_BACKOFF_S,
        "max_backoff_s": MAX_BACKOFF_S,
        **overrides,
    }
    heard.client = TwitchIrcClient(
        "127.0.0.1",
        irc.port,
        channel=CHANNEL,
        token_provider=token_provider,
        nick_provider=nick_provider,
        on_privmsg=on_privmsg,
        on_usernotice=on_usernotice,
        on_connected=on_connected,
        on_disconnected=on_disconnected,
        **options,
    )
    return heard


@pytest.fixture
async def joined(irc: FakeTwitchIrc) -> AsyncIterator[Heard]:
    heard = _client(irc)
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.joined, what="the client to join")
        yield heard
    finally:
        await heard.client.stop()


async def test_the_login_is_pass_nick_cap_join_in_that_order(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    assert irc.connections[0].lines[:4] == [
        "PASS oauth:test-only",
        "NICK noxbot",
        "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership",
        f"JOIN #{CHANNEL}",
    ]
    assert irc.connections[0].capabilities == [
        "twitch.tv/tags",
        "twitch.tv/commands",
        "twitch.tv/membership",
    ]


async def test_a_token_that_already_has_the_prefix_is_not_prefixed_twice(
    irc: FakeTwitchIrc,
) -> None:
    heard = _client(irc, token="oauth:test-only")
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.joined, what="the client to join")
    finally:
        await heard.client.stop()
    assert irc.lines_of("PASS") == ["PASS oauth:test-only"]


async def test_the_join_echo_is_matched_to_the_bots_nick_whatever_its_case(
    irc: FakeTwitchIrc,
) -> None:
    heard = _client(irc, nick="NoxBot")  # the server echoes the nick in lower case
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.joined, what="the client to join")
    finally:
        await heard.client.stop()
    assert heard.connected == 1


async def test_a_server_ping_is_answered_with_the_same_token(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.ping("tmi-check-1")
    await wait_until(lambda: irc.pongs == ["PONG :tmi-check-1"], what="the PONG")


async def test_a_chat_line_reaches_the_handler_with_its_tags_decoded(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.privmsg(
        "viewer_01",
        "hallo zusammen",
        display_name="Viewer_01",
        badges="subscriber/3,premium/1",
        subscriber=True,
        mod=True,
        bits=25,
        reply_to=ReplyTo("NoxBot"),
    )
    await wait_until(lambda: joined.privmsgs, what="the chat line")
    tags, login, channel, text = joined.privmsgs[0]
    assert (login, channel, text) == ("viewer_01", CHANNEL, "hallo zusammen")
    assert tags.display_name == "Viewer_01"
    assert tags.user_id == user_id_for("viewer_01")
    assert tags.badges == ["subscriber/3", "premium/1"]
    assert (tags.subscriber, tags.mod, tags.bits) == (True, True, 25)
    assert tags.reply_parent_login == "noxbot"


async def test_text_with_unicode_and_a_colon_arrives_whole(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.privmsg("viewer_02", "Zeit: 19:30 - äöü ß \U0001f44b")
    await wait_until(lambda: joined.privmsgs, what="the chat line")
    assert joined.privmsgs[0][3] == "Zeit: 19:30 - äöü ß \U0001f44b"


async def test_a_user_notice_reaches_its_handler_with_the_raw_tags(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.usernotice(
        "subgift", "viewer_03", params={"recipient-user-name": "viewer_04", "months": "2"}
    )
    await wait_until(lambda: joined.notices, what="the notice")
    tags, login, channel, _text = joined.notices[0]
    assert (login, channel) == ("viewer_03", CHANNEL)
    assert tags["msg-id"] == "subgift"
    assert tags["msg-param-recipient-user-name"] == "viewer_04"
    assert tags["msg-param-months"] == "2"


async def test_lines_the_client_has_no_use_for_do_not_disturb_it(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.send_raw(f":tmi.twitch.tv CLEARCHAT #{CHANNEL} :viewer_05")
    await irc.send_raw(f"@msg-id=slow_on :tmi.twitch.tv NOTICE #{CHANNEL} :slow mode")
    await irc.send_raw(":tmi.twitch.tv 999 noxbot :something new")
    await irc.privmsg("viewer_06", "danach")  # the marker: handled after all of the above
    await wait_until(lambda: joined.privmsgs, what="the marker line")
    assert joined.client.joined and len(irc.connections) == 1


async def test_a_refused_login_is_a_last_error_and_never_a_join() -> None:
    async with FakeTwitchIrc(valid_token="right") as irc:
        heard = _client(irc, token="wrong")
        heard.client.start()
        try:
            await wait_until(lambda: len(irc.connections) >= 2, what="a second attempt")
            assert "Login authentication failed" in heard.client.last_error
            assert heard.client.joined is False
        finally:
            await heard.client.stop()


async def test_missing_credentials_mean_no_connection_at_all(irc: FakeTwitchIrc) -> None:
    heard = _client(irc, token=None)
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.last_error, what="the reason")
    finally:
        await heard.client.stop()
    assert "must both be set" in heard.client.last_error
    assert irc.connections == []


async def test_the_egress_check_runs_before_every_attempt_and_blocks_the_connection(
    irc: FakeTwitchIrc,
) -> None:
    def refuse() -> None:
        raise PermissionError("egress denied for the test")

    heard = _client(irc, authorize=refuse)
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.last_error, what="the reason")
    finally:
        await heard.client.stop()
    assert "egress denied" in heard.client.last_error
    assert irc.connections == []


async def test_refused_connections_make_the_client_wait_longer_up_to_the_ceiling(
    irc: FakeTwitchIrc,
) -> None:
    irc.refuse_connections(5)
    heard = _client(irc)
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.joined, what="the client to get in")
        assert len(irc.connections) == 6
        # 0.05 -> 0.1 -> 0.2 -> 0.2 -> 0.2: grown to the ceiling and not past it.
        assert heard.client.backoff_s == MAX_BACKOFF_S
    finally:
        await heard.client.stop()


@pytest.mark.parametrize("server_closes", [False, True])
async def test_reconnect_from_twitch_makes_the_client_log_in_again(
    irc: FakeTwitchIrc, joined: Heard, server_closes: bool
) -> None:
    await irc.reconnect(close=server_closes)  # False: the client has to act on the line itself
    await wait_until(lambda: len(irc.connections) == 2, what="the second connection")
    await wait_until(lambda: joined.client.joined, what="the client to join again")
    assert irc.connections[1].lines[:4] == irc.connections[0].lines[:4]
    assert len(joined.disconnected) == 1


async def test_the_client_closes_the_connection_it_was_told_to_leave(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.reconnect(close=False)
    await wait_until(lambda: len(irc.connections) == 2, what="the second connection")
    assert irc.connections[0].closed  # the old line is not left open next to the new one


async def test_a_connection_cut_without_a_word_is_replaced(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await irc.drop_connections()
    await wait_until(lambda: len(irc.connections) == 2, what="the second connection")
    await wait_until(lambda: joined.client.joined, what="the client to join again")
    await irc.privmsg("viewer_07", "wieder da")
    await wait_until(lambda: joined.privmsgs, what="chat on the new connection")


async def test_a_connection_that_goes_quiet_is_replaced_after_the_idle_timeout(
    irc: FakeTwitchIrc,
) -> None:
    heard = _client(irc, idle_timeout_s=1.0)  # long enough that a slow runner never trips it
    heard.client.start()
    try:
        await wait_until(lambda: heard.client.joined, what="the client to join")
        irc.go_silent()  # no more pings, no echo: a path that died without a FIN
        await wait_until(lambda: len(irc.connections) == 2, what="the second connection")
        await wait_until(lambda: heard.client.joined, what="the client to join again")
    finally:
        await heard.client.stop()
    assert "no data from Twitch" in heard.disconnected[0]


async def test_stopping_the_client_closes_its_connection_and_stops_reconnecting(
    irc: FakeTwitchIrc, joined: Heard
) -> None:
    await joined.client.stop()
    await wait_until(lambda: irc.open_connections == [], what="the server noticing the close")
    assert (joined.client.connected, joined.client.joined) == (False, False)
    assert len(irc.connections) == 1


# -- the plugin ---------------------------------------------------------------------------------


@pytest.fixture
async def plugin_env(
    irc: FakeTwitchIrc,
) -> AsyncIterator[tuple[TwitchPlugin, FakeClient, Any]]:
    client = FakeClient()  # token "s3cret", bot login "noxbot"
    api = make_api(
        client,
        port=irc.port,
        logger=RecordingLogger(),
        rate_limit_min_gap_s=0.0,
        rate_limit_max_messages=3,
        rate_limit_window_s=30.0,
    )
    plugin = create(api)
    await plugin.start()
    await wait_until(lambda: plugin.client.joined, what="the plugin to join")
    try:
        yield plugin, client, api
    finally:
        await plugin.stop()


def _events(client: FakeClient, name: str) -> list[dict[str, Any]]:
    return [payload for event, payload in client.events if event == name]


async def test_the_plugin_logs_in_with_the_stored_secrets_and_reports_healthy(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    plugin, client, _api = plugin_env
    assert irc.lines_of("PASS") == ["PASS oauth:s3cret"]
    assert irc.lines_of("NICK") == ["NICK noxbot"]
    assert _events(client, "twitch.connected") == [{"reason": ""}]
    assert (await plugin.health())[0] is HealthStatus.AVAILABLE


async def test_chat_becomes_events_and_a_command_gets_its_answer_in_chat(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    _plugin, client, _api = plugin_env
    await irc.privmsg("viewer_01", "!befehle")
    await wait_until(lambda: irc.delivered, what="the answer in chat")
    assert irc.delivered[0].text == command_list("de")
    (chat,) = _events(client, "twitch.chat_message")
    assert chat["viewer_id"] == user_id_for("viewer_01")
    assert chat["text"] == "!befehle"
    assert _events(client, "twitch.command_invoked")[0]["command"] == "befehle"


async def test_a_reply_to_the_bot_is_addressed_to_nox(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    _plugin, client, _api = plugin_env
    await irc.privmsg("viewer_01", "genau das", reply_to=ReplyTo("noxbot"))
    await wait_until(lambda: _events(client, "twitch.chat_message"), what="the chat event")
    assert _events(client, "twitch.chat_message")[0]["addressed_to_nox"] is True


async def test_subs_gifts_and_raids_ask_for_funken_and_a_mass_gift_counts_once(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    _plugin, client, _api = plugin_env
    await irc.usernotice("sub", "viewer_08")
    await irc.usernotice("submysterygift", "viewer_09", params={"mass-gift-count": "3"})
    for number in range(3):
        await irc.usernotice(
            "subgift",
            "viewer_09",
            params={"community-gift-id": "1", "recipient-user-name": f"viewer_1{number}"},
        )
    await irc.usernotice("raid", "raider_01", params={"viewerCount": "24"})
    await wait_until(
        lambda: len(_events(client, "stream.funken_awarded")) == 3, what="three requests"
    )
    awards = _events(client, "stream.funken_awarded")
    assert [(a["viewer_id"], a["reason"], a["units"]) for a in awards] == [
        (user_id_for("viewer_08"), "sub", 1.0),
        (user_id_for("viewer_09"), "sub", 3.0),
        (user_id_for("raider_01"), "raid", 1.0),
    ]


async def test_the_plugin_keeps_to_its_own_send_budget_before_twitch_ever_sees_a_message(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    plugin, _client, _api = plugin_env
    for number in range(3):
        await plugin._send(f"Nachricht {number}")
    with pytest.raises(IpcError, match="rate limited"):
        await plugin._send("eine zu viel")
    await wait_until(lambda: len(irc.delivered) == 3, what="three delivered messages")
    assert irc.refused_for_rate == []  # Twitch never had to say no
    assert [d.text for d in irc.delivered] == ["Nachricht 0", "Nachricht 1", "Nachricht 2"]


async def test_a_refusal_from_twitch_does_not_disturb_the_plugin_and_sending_resumes() -> None:
    now = [100.0]
    async with FakeTwitchIrc(send_limit=2, send_window_s=30.0, clock=lambda: now[0]) as irc:
        client = FakeClient()
        api = make_api(
            client,
            port=irc.port,
            logger=RecordingLogger(),
            rate_limit_min_gap_s=0.0,
            rate_limit_max_messages=10,  # more than Twitch is willing to take
        )
        plugin = create(api)
        await plugin.start()
        try:
            await wait_until(lambda: plugin.client.joined, what="the plugin to join")
            for number in range(3):
                await plugin._send(f"Nachricht {number}")
            await wait_until(lambda: irc.refused_for_rate, what="Twitch refusing the third")
            assert plugin.client.joined is True
            assert len(irc.connections) == 1  # a refusal is not a reason to reconnect

            now[0] += 30.0  # Twitch's window has passed
            await plugin._send("danach")
            await wait_until(lambda: len(irc.delivered) == 3, what="the message after the window")
            assert irc.delivered[-1].text == "danach"
        finally:
            await plugin.stop()


async def test_a_message_twitch_calls_a_duplicate_is_a_refusal_not_an_error(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    plugin, _client, _api = plugin_env
    await plugin._send("gleich")
    await plugin._send("gleich")
    await wait_until(lambda: irc.refused_as_duplicate, what="Twitch refusing the duplicate")
    assert [d.text for d in irc.delivered] == ["gleich"]
    assert plugin.client.joined is True


async def test_losing_the_connection_is_reported_and_nothing_is_queued_for_later(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    plugin, client, _api = plugin_env
    irc.refuse_connections(10_000)
    await irc.drop_connections()
    await wait_until(lambda: not plugin.client.connected, what="the plugin to notice")
    assert len(_events(client, "twitch.disconnected")) == 1
    assert (await plugin.health())[0] is HealthStatus.UNAVAILABLE
    with pytest.raises(ConnectionError, match="not connected to Twitch"):
        await plugin._send("während des Ausfalls")

    irc.refuse_connections(0)
    await wait_until(lambda: plugin.client.joined, what="the plugin to come back")
    await irc.privmsg("viewer_01", "marker")  # handled after the reconnect
    await wait_until(lambda: _events(client, "twitch.chat_message"), what="the marker")
    assert irc.delivered == []  # the message sent during the outage was not kept and re-sent


async def test_a_refused_login_is_the_health_reason() -> None:
    async with FakeTwitchIrc(valid_token="right") as irc:
        client = FakeClient(oauth_token="wrong")
        plugin = create(make_api(client, port=irc.port, logger=RecordingLogger()))
        await plugin.start()
        try:
            await wait_until(lambda: len(irc.connections) >= 2, what="a second attempt")
            health, reason = await plugin.health()
            assert health is HealthStatus.UNAVAILABLE
            assert "Login authentication failed" in reason
        finally:
            await plugin.stop()


async def test_missing_secrets_are_the_health_reason_and_nothing_connects(
    irc: FakeTwitchIrc,
) -> None:
    client = FakeClient(oauth_token=None, bot_username=None)
    plugin = create(make_api(client, port=irc.port, logger=RecordingLogger()))
    await plugin.start()
    try:
        health, reason = await plugin.health()
        assert health is HealthStatus.UNAVAILABLE
        assert "no credentials configured" in reason
        await wait_until(lambda: plugin.client.last_error, what="the client's reason")
    finally:
        await plugin.stop()
    assert irc.connections == []


async def test_stopping_the_plugin_leaves_nothing_connected(
    irc: FakeTwitchIrc, plugin_env: tuple[TwitchPlugin, FakeClient, Any]
) -> None:
    plugin, _client, _api = plugin_env
    await plugin.stop()
    await wait_until(lambda: irc.open_connections == [], what="the connection to close")
    assert plugin.client._task is None  # the reconnect loop is gone, not just paused
    assert len(irc.connections) == 1
