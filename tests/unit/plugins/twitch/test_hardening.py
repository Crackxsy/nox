"""Hardening of what Nox sends to Twitch chat and of who Nox listens to:

- the link and command-prefix rules judge the text that is really sent, not only the text that came
  in (a dropped `@` can glue two words into a domain), and an `@` only counts as a mention at the
  start of a word;
- the moderation gate sees through look-alike letters, full-width forms, combining marks and
  invisible characters;
- a reply to the bot's own message is recognised on its own (no name in the text needed);
- other bots and the bot's own account are ignored for chat lines and for notices alike;
- a connection that dies in the middle of a reply is neither retried nor answered twice.

Everything runs against the plugin objects directly or the loopback fake server; no sleeps, no
clock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nox_plugin_twitch import create
from nox_plugin_twitch import outbound as outbound_module
from nox_plugin_twitch.moderation import ModerationGate
from nox_plugin_twitch.outbound import (
    BLOCKED_COMMAND_PREFIX,
    BLOCKED_LINK,
    MAX_MENTIONS,
    filter_outbound,
    matching_variants,
)
from nox_plugin_twitch.plugin import ChatSendInput, TwitchPlugin
from nox_plugin_twitch.protocol import ChatTags, parse_chat_tags

from nox.ipc.errors import IpcError

from .conftest import FakeClient, RecordingLogger, make_api
from .fake_irc_server import FakeIrcServer
from .test_irc_client import _wait_until

pytestmark = pytest.mark.timeout(30)

#: Characters that look like a Latin letter or are not visible, written as code points so that this
#: file holds no look-alike character itself.
CYRILLIC_A = chr(0x430)
CYRILLIC_E = chr(0x435)
CYRILLIC_O = chr(0x43E)
CYRILLIC_U = chr(0x443)
CYRILLIC_JE = chr(0x458)
FULLWIDTH_K = chr(0xFF4B)
FULLWIDTH_Y = chr(0xFF59)
FULLWIDTH_S = chr(0xFF53)
COMBINING_ACUTE = chr(0x301)
COMBINING_DIAERESIS = chr(0x308)
ZERO_WIDTH_SPACE = chr(0x200B)
U_UMLAUT = chr(0xFC)
A_UMLAUT = chr(0xE4)
O_UMLAUT = chr(0xF6)
SHARP_S = chr(0xDF)


# -- the rules judge the line that is sent ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Two mentions are fine, the third loses its `@` - and "foo dot net" is a domain.
        "@a @b foo dot @net",
        "@a @b foo dot @com",
        "@viewer_01 @viewer_02 name punkt @org",
        # The same with spaces around a dot: "name . @gg" would only be a link once the `@` is gone.
        "@a @b name . @gg",
    ],
)
def test_a_link_that_only_appears_once_the_extra_mention_lost_its_at_sign_is_refused(
    text: str,
) -> None:
    result = filter_outbound(text)
    assert result.blocked == BLOCKED_LINK
    assert result.text == ""


def test_the_same_words_with_a_mention_still_inside_the_cap_are_sent() -> None:
    """Only the glued form is a link: a single mention keeps its `@` and the line goes out."""
    result = filter_outbound("foo dot @com")
    assert result.blocked == ""
    assert result.text == "foo dot @com"
    assert result.changes == ()


def test_the_command_prefix_rule_also_judges_the_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the mention step turns the line into is checked again before it can be sent."""
    monkeypatch.setattr(outbound_module, "_cap_mentions", lambda text: "/ban viewer_01")
    result = filter_outbound("@everyone hello")
    assert result.blocked == BLOCKED_COMMAND_PREFIX
    assert result.text == ""


def test_the_link_rule_also_judges_the_final_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbound_module, "_cap_mentions", lambda text: "see example.invalid")
    result = filter_outbound("@everyone hello")
    assert result.blocked == BLOCKED_LINK


def test_an_at_sign_inside_a_word_is_no_mention() -> None:
    # `alex@host` pings nobody, so it neither counts toward the cap nor loses its `@`: the third
    # real mention (`@c`) is the one that loses it.
    result = filter_outbound("mail alex@host and @a @b @c")
    assert MAX_MENTIONS == 2
    assert result.text == "mail alex@host and @a @b c"
    assert result.changes == ("mentions_capped",)


def test_an_at_sign_right_after_a_dot_is_no_mention() -> None:
    # Not a mention, so "@everyone" keeps its `@` here and the words are not glued into a domain.
    result = filter_outbound("wait.@everyone")
    assert result.blocked == ""
    assert result.text == "wait.@everyone"
    assert result.changes == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@everyone hi", "everyone hi"),
        ("(@everyone) hi", "(everyone) hi"),
        ("hi @here", "hi here"),
        ("hallo @viewer_01 @viewer_02 @viewer_03", "hallo @viewer_01 @viewer_02 viewer_03"),
        ("\n@viewer_01\n", "@viewer_01"),
    ],
)
def test_mentions_at_the_start_of_a_word_are_still_capped(text: str, expected: str) -> None:
    assert filter_outbound(text).text == expected


# -- the moderation gate folds what the output filter folds ----------------------------------------

LOOKALIKE_ROWS = [
    ("k" + CYRILLIC_U + "s", "illness_or_death_joke"),
    (FULLWIDTH_K + FULLWIDTH_Y + FULLWIDTH_S, "illness_or_death_joke"),
    ("k" + "y" + COMBINING_ACUTE + "s", "illness_or_death_joke"),
    ("ky" + ZERO_WIDTH_SPACE + "s", "illness_or_death_joke"),
    ("kill y" + CYRILLIC_O + "urself", "illness_or_death_joke"),
    ("h" + "a" + COMBINING_DIAERESIS + "ng dich auf", "illness_or_death_joke"),
    ("h" + A_UMLAUT + "ng dich auf", "illness_or_death_joke"),
    ("hey @viewer_01 send nud" + CYRILLIC_E + "s", "sexual_content_about_viewer"),
    ("@viewer_01 you idi" + CYRILLIC_O + "t", "insult_aimed_at_viewer"),
    ("r" + CYRILLIC_E + "al name is Sam Example", "doxxing"),
    ("call " + chr(0xFF15) * 3 + "-" + chr(0xFF11) * 3 + "-" + chr(0xFF14) * 4, "doxxing"),
    ("12 Main str" + CYRILLIC_E + CYRILLIC_E + "t", "doxxing"),
    (CYRILLIC_JE + "ews should die", "hate_speech_protected_group"),
    ("jews should d" + "i" + CYRILLIC_E, "hate_speech_protected_group"),
]


@pytest.mark.parametrize(("text", "reason"), LOOKALIKE_ROWS)
def test_the_gate_sees_through_lookalikes_full_width_forms_and_marks(
    text: str, reason: str
) -> None:
    ok, got = ModerationGate().check(text)
    assert (ok, got) == (False, reason)


def test_the_gate_sees_through_a_lookalike_in_a_blocklist_word() -> None:
    gate = ModerationGate(blocklist=["forbiddenword"])
    ok, reason = gate.check("this has forbidd" + CYRILLIC_E + "nword in it")
    assert (ok, reason) == (False, "blocklist:forbiddenword")


def test_a_blocklist_word_with_an_umlaut_matches_its_decomposed_spelling() -> None:
    gate = ModerationGate(blocklist=["gr" + U_UMLAUT + "n"])
    ok, reason = gate.check("so gr" + "u" + COMBINING_DIAERESIS + "n wie nie")
    assert (ok, reason) == (False, "blocklist:gr" + U_UMLAUT + "n")


@pytest.mark.parametrize(
    "text",
    [
        "Sch" + O_UMLAUT + "ne Gr" + U_UMLAUT + SHARP_S + "e an Zo" + chr(0xEB) + " und " + "Alex",
        "Thanks for the raid, welcome everyone!",
        "Das war toll, Zo" + chr(0xEB) + "!",
    ],
)
def test_harmless_text_with_accents_and_other_scripts_still_passes(text: str) -> None:
    assert ModerationGate().check(text) == (True, "")


def test_matching_variants_hold_the_text_as_written_composed_and_folded() -> None:
    text = "H" + "a" + COMBINING_DIAERESIS + "ng " + CYRILLIC_U + "s"
    variants = matching_variants(text)
    assert variants[0] == text.lower()
    assert "h" + A_UMLAUT + "ng " + CYRILLIC_U + "s" in variants  # composed
    assert "hang ys" in variants  # folded
    assert len(variants) == len(set(variants))


async def test_a_lookalike_line_is_refused_by_the_gate_on_the_send_path(
    plugin: TwitchPlugin, sent: list[str]
) -> None:
    with pytest.raises(IpcError, match="moderation gate: illness_or_death_joke"):
        await plugin.chat_send(ChatSendInput(text="lol k" + CYRILLIC_U + "s"))
    assert sent == []


# -- fixtures for the plugin tests below ----------------------------------------


@pytest.fixture
def sent() -> list[str]:
    return []


@pytest.fixture
def events_client() -> FakeClient:
    return FakeClient()  # the bot's login is "noxbot"


@pytest.fixture
def plugin(events_client: FakeClient, sent: list[str]) -> TwitchPlugin:
    """A plugin that never connects: what it would send goes into `sent`."""
    api = make_api(events_client, port=1, logger=RecordingLogger(), rate_limit_min_gap_s=0.0)
    created = create(api)

    async def record(text: str) -> None:
        sent.append(text)

    created.client.send_privmsg = record  # type: ignore[method-assign]
    return created


def _chat_events(client: FakeClient) -> list[dict[str, Any]]:
    return [payload for name, payload in client.events if name == "twitch.chat_message"]


def _awards(client: FakeClient) -> list[dict[str, Any]]:
    return [payload for name, payload in client.events if name == "stream.funken_awarded"]


def _tags(**raw: str) -> ChatTags:
    return parse_chat_tags({"user-id": "42", **raw})


# -- a reply to the bot is recognised on its own ----------------------------------------


@pytest.mark.parametrize(
    ("raw_tags", "addressed", "relevance"),
    [
        ({"reply-parent-user-login": "noxbot"}, True, 0.6),
        ({"reply-parent-user-login": "NoxBot"}, True, 0.6),  # the server's spelling does not matter
        ({"reply-parent-user-login": "  noxbot "}, True, 0.6),
        ({"reply-parent-user-login": "viewer_02"}, False, 0.0),
        ({"reply-parent-user-login": ""}, False, 0.0),
        ({"reply-parent-msg-id": "abc"}, False, 0.0),  # a reply tag of another kind says nothing
        ({}, False, 0.0),
    ],
)
async def test_a_reply_to_the_bot_is_addressed_without_a_name_or_a_question(
    plugin: TwitchPlugin,
    events_client: FakeClient,
    raw_tags: dict[str, str],
    addressed: bool,
    relevance: float,
) -> None:
    # "genau das": no name, no question mark, no greeting - only the reply tag can address Nox.
    await plugin._on_privmsg(_tags(**raw_tags), "viewer_01", "testchannel", "genau das")
    (payload,) = _chat_events(events_client)
    assert payload["addressed_to_nox"] is addressed
    assert payload["relevance"] == pytest.approx(relevance)


async def test_without_a_bot_login_a_plain_message_is_never_a_reply_to_the_bot() -> None:
    """No credentials yet: both logins are "", and "" must not count as "the same login"."""
    client = FakeClient(bot_username=None)
    api = make_api(client, port=1, logger=RecordingLogger())
    plugin = create(api)
    await plugin._on_privmsg(_tags(), "viewer_01", "testchannel", "genau das")
    (payload,) = _chat_events(client)
    assert payload["addressed_to_nox"] is False
    assert payload["relevance"] == 0.0


async def test_the_reply_tag_is_read_from_a_real_privmsg_line() -> None:
    """From the wire to the flag: `parse_line` -> tags -> `ChatTags.reply_parent_login`."""
    from nox_plugin_twitch.protocol import parse_line

    line = (
        "@display-name=Viewer_01;user-id=42;reply-parent-user-login=NoxBot;reply-parent-msg-id=x "
        ":viewer_01!viewer_01@viewer_01.tmi.twitch.tv PRIVMSG #testchannel :genau das"
    )
    message = parse_line(line)
    assert parse_chat_tags(message.tags).reply_parent_login == "noxbot"


# -- other bots and Nox's own account are ignored ----------------------------------------


@pytest.mark.parametrize("login", ["nightbot", "NightBot", "streamelements", "noxbot", "NoxBot"])
async def test_chat_from_other_bots_and_from_the_own_account_is_dropped_completely(
    plugin: TwitchPlugin, events_client: FakeClient, sent: list[str], login: str
) -> None:
    await plugin._on_privmsg(_tags(bits="50"), login, "testchannel", "!befehle")
    await plugin._on_privmsg(_tags(), login, "testchannel", "hallo zusammen")
    assert events_client.events == []  # no chat event, no command, no award
    assert sent == []


async def test_an_ordinary_viewer_is_still_heard(
    plugin: TwitchPlugin, events_client: FakeClient, sent: list[str]
) -> None:
    await plugin._on_privmsg(_tags(), "viewer_01", "testchannel", "!befehle")
    assert len(_chat_events(events_client)) == 1
    assert len(sent) == 1


@pytest.mark.parametrize("login", ["nightbot", "NightBot", "noxbot", "NoxBot"])
@pytest.mark.parametrize(
    "msg_id", ["sub", "resub", "subgift", "submysterygift", "raid"], ids=lambda value: value
)
async def test_notices_from_other_bots_and_from_the_own_account_earn_nothing(
    plugin: TwitchPlugin, events_client: FakeClient, login: str, msg_id: str
) -> None:
    tags = {"msg-id": msg_id, "user-id": "42", "msg-param-mass-gift-count": "3"}
    await plugin._on_usernotice(tags, login, "testchannel", "")
    assert _awards(events_client) == []


async def test_a_notice_from_a_viewer_still_earns_funken(
    plugin: TwitchPlugin, events_client: FakeClient
) -> None:
    await plugin._on_usernotice({"msg-id": "sub", "user-id": "42"}, "viewer_01", "testchannel", "")
    assert [a["reason"] for a in _awards(events_client)] == ["sub"]


async def test_a_notice_without_a_login_is_not_taken_for_the_bot() -> None:
    """With no credentials the bot's login is "" - a notice with no login must not match it."""
    client = FakeClient(bot_username=None)
    plugin = create(make_api(client, port=1, logger=RecordingLogger()))
    await plugin._on_usernotice({"msg-id": "sub", "user-id": "42"}, "", "testchannel", "")
    assert [a["reason"] for a in _awards(client)] == ["sub"]


# -- a connection that dies in the middle of a reply ----------------------------------------


class DroppingIrcServer(FakeIrcServer):
    """Closes the connection as soon as it has read a PRIVMSG line, like a network that fails
    between the bot's write and the server's answer."""

    def __init__(self) -> None:
        super().__init__()
        self.drop_next_privmsg = True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.received_lines.append(line)
                if line.startswith("NICK "):
                    self._nick = line[len("NICK ") :].strip()
                elif line.startswith("JOIN "):
                    channel = line[len("JOIN ") :].strip()
                    self._joined_channel = channel
                    prefix = f"{self._nick}!{self._nick}@{self._nick}.tmi.twitch.tv"
                    await self._write(writer, f":{prefix} JOIN {channel}")
                elif line.startswith("PRIVMSG ") and self.drop_next_privmsg:
                    self.drop_next_privmsg = False
                    return  # the finally block closes the connection
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()


def _privmsgs(server: FakeIrcServer) -> list[str]:
    return [line for line in server.received_lines if line.startswith("PRIVMSG ")]


def _joins(server: FakeIrcServer) -> int:
    return sum(1 for line in server.received_lines if line.startswith("JOIN "))


async def test_a_reply_cut_off_by_a_dropped_connection_is_not_sent_again() -> None:
    server = DroppingIrcServer()
    await server.start()
    client = FakeClient()
    plugin = create(make_api(client, port=server.port, rate_limit_min_gap_s=0.0))
    await plugin.start()
    try:
        await _wait_until(lambda: plugin.client.joined)
        await plugin._send("gg viewer_01")
        await _wait_until(lambda: len(_privmsgs(server)) == 1)
        await _wait_until(lambda: _joins(server) >= 2 and plugin.client.joined)
        # The client came back, joined again, and did not repeat the line.
        assert _privmsgs(server) == ["PRIVMSG #testchannel :gg viewer_01"]
        assert plugin.client.connected
    finally:
        await plugin.stop()
        await server.stop()


class _BrokenWriter:
    """A writer whose connection is gone: `write` is accepted, `drain` reports the reset."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.drains = 0

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drains += 1
        raise ConnectionResetError("connection reset by peer")


async def test_a_write_that_fails_is_reported_once_and_never_retried(
    events_client: FakeClient,
) -> None:
    api = make_api(
        events_client,
        port=1,
        logger=RecordingLogger(),
        rate_limit_max_messages=1,
        rate_limit_min_gap_s=0.0,
    )
    plugin = create(api)
    writer = _BrokenWriter()
    plugin.client._writer = writer  # type: ignore[assignment]
    plugin.client.connected = True

    with pytest.raises(ConnectionResetError):
        await plugin._send("gg viewer_01")

    assert writer.writes == [b"PRIVMSG #testchannel :gg viewer_01\r\n"]  # one attempt, one line
    assert writer.drains == 1
    # The failed attempt used the only slot of the send budget: failing does not make sending free.
    with pytest.raises(IpcError, match="rate limited"):
        await plugin._send("gg again")
    assert len(writer.writes) == 1
