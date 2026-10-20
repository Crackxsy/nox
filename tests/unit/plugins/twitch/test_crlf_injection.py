"""CRLF injection: nothing built from data may put a second IRC command on the wire.

An IRC line ends at the first CR/LF, so a chat text like `hi\\r\\nJOIN #other` would make the bot
run `JOIN #other`. Every layer refuses it on its own: the `twitch.chat.send` input model, the
plugin's send path, `TwitchIrcClient.send_privmsg` / `_send_raw`, the channel/nick/token checks,
and the configuration (`stream.ai_voice_disclosure.chat_text`, `stream.twitch.channel`) on load.
"""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_twitch import create
from nox_plugin_twitch.irc_client import IrcInjectionError, TwitchIrcClient, has_line_break
from nox_plugin_twitch.plugin import ChatSendInput
from pydantic import ValidationError

from nox.core.config.features import StreamAiVoiceDisclosureConfig, StreamTwitchConfig

from .conftest import FakeClient, make_api
from .fake_irc_server import FakeIrcServer

pytestmark = pytest.mark.timeout(30)

INJECTIONS = ["hi\r\nJOIN #other", "hi\nPRIVMSG #x :spam", "hi\rQUIT", "hi\x00there"]


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")


def _client(
    irc_server: FakeIrcServer,
    *,
    channel: str = "testchannel",
    nick: str = "noxbot",
    token: str = "s3cret",
) -> TwitchIrcClient:
    async def _token() -> str:
        return token

    async def _nick() -> str:
        return nick

    async def _on_privmsg(_tags, _login, _channel, _text):
        return None

    return TwitchIrcClient(
        "127.0.0.1",
        irc_server.port,
        channel=channel,
        token_provider=_token,
        nick_provider=_nick,
        on_privmsg=_on_privmsg,
        tls=False,
        min_backoff_s=0.05,
        max_backoff_s=0.2,
    )


def _sent_injected(irc_server: FakeIrcServer) -> bool:
    return any(
        line.startswith(("JOIN #other", "QUIT")) or line.startswith("PRIVMSG #x")
        for line in irc_server.received_lines
    )


# ---- the client -------------------------------------------------------------------------------


@pytest.mark.parametrize("text", INJECTIONS)
def test_has_line_break_spots_cr_lf_and_nul(text: str) -> None:
    assert has_line_break(text)
    assert not has_line_break("a perfectly normal line - with ümlauts and :colons:")


@pytest.mark.parametrize("text", INJECTIONS)
async def test_send_privmsg_refuses_line_breaks(irc_server: FakeIrcServer, text: str) -> None:
    client = _client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        with pytest.raises(IrcInjectionError):
            await client.send_privmsg(text)
        await client.send_privmsg("marker")
        marker = "PRIVMSG #testchannel :marker"
        await _wait_until(lambda: any(marker in line for line in irc_server.received_lines))
        assert not _sent_injected(irc_server)
        assert not any("hi" in line for line in irc_server.received_lines if "PRIVMSG" in line)
    finally:
        await client.stop()


async def test_send_raw_is_the_last_line_of_defence(irc_server: FakeIrcServer) -> None:
    client = _client(irc_server)
    client.start()
    try:
        await _wait_until(lambda: client.joined)
        with pytest.raises(IrcInjectionError):
            await client._send_raw("PRIVMSG #testchannel :a\r\nQUIT")
        assert not _sent_injected(irc_server)
    finally:
        await client.stop()


@pytest.mark.parametrize("channel", ["test channel", "a,#other", "x\r\nQUIT", "x\x00", ":x"])
def test_a_channel_that_would_split_join_is_refused(
    irc_server: FakeIrcServer, channel: str
) -> None:
    with pytest.raises(IrcInjectionError):
        _client(irc_server, channel=channel)


@pytest.mark.parametrize(
    ("nick", "token"),
    [("nox\r\nJOIN #other", "s3cret"), ("noxbot", "s3cret\r\nJOIN #other"), ("nox bot", "s3cret")],
)
async def test_nick_and_token_with_line_breaks_never_reach_the_server(
    irc_server: FakeIrcServer, nick: str, token: str
) -> None:
    client = _client(irc_server, nick=nick, token=token)
    client.start()
    try:
        await _wait_until(lambda: bool(client.last_error))
        assert client.connected is False
        assert irc_server.received_lines == []
        assert "s3cret" not in client.last_error  # the error names the field, never the secret
    finally:
        await client.stop()


# ---- the tool -----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", INJECTIONS)
def test_chat_send_input_rejects_line_breaks(text: str) -> None:
    with pytest.raises(ValidationError):
        ChatSendInput(text=text)
    assert ChatSendInput(text="gg everyone").text == "gg everyone"


@pytest.mark.parametrize("text", INJECTIONS)
async def test_the_tool_never_sends_an_injected_line(
    irc_server: FakeIrcServer, fake_client: FakeClient, text: str
) -> None:
    api = make_api(fake_client, port=irc_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await _wait_until(lambda: plugin.client.joined)
        with pytest.raises(Exception):  # noqa: B017 - validation error type depends on the layer
            await api.tools.call("twitch.chat.send", {"text": text})
        with pytest.raises(Exception, match="one line"):
            await plugin._send(text)  # the send path behind the built-in commands, too
        assert not _sent_injected(irc_server)
    finally:
        await plugin.stop()


# ---- the configuration --------------------------------------------------------------------------


@pytest.mark.parametrize("text", INJECTIONS)
def test_disclosure_chat_text_with_line_breaks_is_rejected_on_load(text: str) -> None:
    with pytest.raises(ValidationError, match="single line"):
        StreamAiVoiceDisclosureConfig.model_validate({"chat_text": text})
    with pytest.raises(ValidationError, match="single line"):
        StreamAiVoiceDisclosureConfig.model_validate({"overlay_label": "KI\nStimme"})
    ok = StreamAiVoiceDisclosureConfig.model_validate({"chat_text": "Synthetic voice!"})
    assert ok.chat_text == "Synthetic voice!"


@pytest.mark.parametrize("channel", ["test channel", "a,#b", "x\r\nQUIT", "x\x00", "a" * 26])
def test_twitch_channel_must_be_a_login_name(channel: str) -> None:
    with pytest.raises(ValidationError, match="Twitch login name"):
        StreamTwitchConfig.model_validate({"channel": channel})


@pytest.mark.parametrize("channel", ["", "testchannel", "#Some_Channel42", "  padded  "])
def test_valid_twitch_channels_still_load(channel: str) -> None:
    assert StreamTwitchConfig.model_validate({"channel": channel}).channel == channel.strip()


def test_bot_names_with_line_breaks_are_rejected() -> None:
    with pytest.raises(ValidationError, match="line breaks"):
        StreamTwitchConfig.model_validate({"bot_names": ["nox\r\nQUIT"]})
