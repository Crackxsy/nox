"""`twitch.*` tools, health, and the built-in chat commands, registered through `api.tools.register`
exactly like every other plugin (`create(api)` -> `api.tools.call(name, payload)`), against the
fake IRC server."""

from __future__ import annotations

import asyncio

import pytest
import structlog
from nox_plugin_twitch import create

from nox.core.events import HealthStatus

from .conftest import FakeClient, make_api
from .fake_irc_server import FakeIrcServer
from .test_irc_client import _wait_until

pytestmark = pytest.mark.timeout(30)


async def _wait_joined(plugin, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if plugin.client.joined:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("plugin never joined the fake IRC channel")


async def _wait_event(client: FakeClient, name: str, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for event_name, payload in client.events:
            if event_name == name:
                return payload
        await asyncio.sleep(0.02)
    raise AssertionError(f"event {name!r} was never emitted; seen: {client.events}")


async def _plugin(irc_server: FakeIrcServer, fake_client: FakeClient, **config_overrides):
    api = make_api(fake_client, port=irc_server.port, **config_overrides)
    plugin = create(api)
    await plugin.start()
    await _wait_joined(plugin)
    return api, plugin


async def test_tools_are_registered_with_the_manifest_risk(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        assert api.tools.names() == ["twitch.chat.send", "twitch.chat.status.read"]
        assert api.tools.get("twitch.chat.send").risk.value == "low"
        assert api.tools.get("twitch.chat.status.read").risk.value == "read"
    finally:
        await plugin.stop()


async def test_chat_status_read_reports_connected_and_joined(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        result = await api.tools.call("twitch.chat.status.read", {})
        assert result == {
            "connected": True,
            "joined": True,
            "channel": "testchannel",
            "reason": "",
        }
    finally:
        await plugin.stop()


async def test_chat_send_delivers_the_message(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        result = await api.tools.call("twitch.chat.send", {"text": "gg everyone"})
        assert result == {"sent": True, "text": "gg everyone"}
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if any(
                "PRIVMSG #testchannel :gg everyone" in line for line in irc_server.received_lines
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("server never received the PRIVMSG")
    finally:
        await plugin.stop()


async def test_chat_send_is_blocked_by_the_moderation_gate(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        with pytest.raises(Exception, match="moderation gate"):
            await api.tools.call("twitch.chat.send", {"text": "kys already"})
    finally:
        await plugin.stop()


def _privmsgs(server: FakeIrcServer) -> list[str]:
    return [line for line in server.received_lines if line.startswith("PRIVMSG ")]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("/ban viewer_01", "command_prefix"),
        (". /ban viewer_01", "command_prefix"),
        ("!lurk", "command_prefix"),
        ("visit example.invalid for more", "link"),
        ("https://example.invalid", "link"),
    ],
)
async def test_chat_send_is_blocked_by_the_output_filter(
    irc_server: FakeIrcServer, fake_client: FakeClient, text: str, reason: str
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        with structlog.testing.capture_logs() as logs:
            with pytest.raises(Exception, match=f"output filter: {reason}"):
                await api.tools.call("twitch.chat.send", {"text": text})
        await asyncio.sleep(0.1)
        assert _privmsgs(irc_server) == []
        blocked = [entry for entry in logs if entry["event"] == "twitch.outbound_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == reason
        assert blocked[0]["chars"] == len(text)
        assert text not in repr(logs), "the log names the reason and the length, never the text"
    finally:
        await plugin.stop()


async def test_a_refused_line_does_not_use_up_the_rate_limit(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(
        irc_server,
        fake_client,
        rate_limit_max_messages=1,
        rate_limit_window_s=30.0,
        rate_limit_min_gap_s=0.0,
    )
    try:
        with pytest.raises(Exception, match="output filter"):
            await api.tools.call("twitch.chat.send", {"text": "/ban viewer_01"})
        assert await api.tools.call("twitch.chat.send", {"text": "ok"}) == {
            "sent": True,
            "text": "ok",
        }
    finally:
        await plugin.stop()


async def test_chat_send_delivers_the_cleaned_line(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        result = await api.tools.call(
            "twitch.chat.send", {"text": "Hal\u200blo  @everyone und @viewer_01"}
        )
        assert result == {"sent": True, "text": "Hallo everyone und @viewer_01"}
        await _wait_until(lambda: len(_privmsgs(irc_server)) == 1)
        assert _privmsgs(irc_server) == ["PRIVMSG #testchannel :Hallo everyone und @viewer_01"]
    finally:
        await plugin.stop()


async def test_the_output_filter_runs_before_the_moderation_gate_sees_the_line(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    """A blocklist word split by a zero-width character is still caught: the gate reads the
    cleaned line."""
    api, plugin = await _plugin(irc_server, fake_client, moderation_blocklist=["forbidden"])
    try:
        with pytest.raises(Exception, match="moderation gate"):
            await api.tools.call("twitch.chat.send", {"text": "a for\u200bbidden word"})
    finally:
        await plugin.stop()


async def test_chat_send_is_rate_limited(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(
        irc_server,
        fake_client,
        rate_limit_max_messages=2,
        rate_limit_window_s=30.0,
        rate_limit_min_gap_s=0.0,
    )
    try:
        await api.tools.call("twitch.chat.send", {"text": "one"})
        await api.tools.call("twitch.chat.send", {"text": "two"})
        with pytest.raises(Exception, match="rate limit"):
            await api.tools.call("twitch.chat.send", {"text": "three"})
    finally:
        await plugin.stop()


async def test_health_available_once_joined(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        status, reason = await plugin.health()
        assert status is HealthStatus.AVAILABLE
        assert reason == "connected"
    finally:
        await plugin.stop()


async def test_health_unavailable_without_credentials(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    no_creds_client = FakeClient(oauth_token=None, bot_username=None)
    api = make_api(no_creds_client, port=irc_server.port)
    plugin = create(api)
    await plugin.start()
    try:
        await asyncio.sleep(0.2)
        status, reason = await plugin.health()
        assert status is HealthStatus.UNAVAILABLE
        assert "credentials" in reason
    finally:
        await plugin.stop()


async def test_chat_message_event_carries_relevance_and_addressed(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        await irc_server.send_privmsg(text="hey nox how are you?", user_id="1", login="viewer1")
        payload = await _wait_event(fake_client, "twitch.chat_message")
        assert payload["text"] == "hey nox how are you?"
        assert payload["viewer_id"] == "1"
        assert payload["channel"] == "public"
        assert payload["addressed_to_nox"] is True
        assert payload["relevance"] > 0.5
    finally:
        await plugin.stop()


async def test_command_invoked_event_is_emitted_for_every_command(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        await irc_server.send_privmsg(text="!hilfe", user_id="2", login="viewer2")
        payload = await _wait_event(fake_client, "twitch.command_invoked")
        assert payload == {
            "chat_event_id": 1,
            "viewer_id": "2",
            "command": "hilfe",
            "args": [],
        }
    finally:
        await plugin.stop()


async def test_hilfe_replies_in_german_and_help_in_english(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client, rate_limit_min_gap_s=0.0)
    try:
        await irc_server.send_privmsg(text="!hilfe", user_id="2", login="viewer2")
        await asyncio.sleep(0.2)
        assert any("Befehle:" in line for line in irc_server.received_lines)
        await irc_server.send_privmsg(text="!help", user_id="3", login="viewer3")
        await asyncio.sleep(0.2)
        assert any("Commands:" in line for line in irc_server.received_lines)
    finally:
        await plugin.stop()


async def test_funken_command_never_sends_a_fabricated_balance(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    try:
        await irc_server.send_privmsg(text="!funken", user_id="4", login="viewer4")
        await _wait_event(fake_client, "twitch.command_invoked")
        await asyncio.sleep(0.2)
        assert not any("PRIVMSG" in line for line in irc_server.received_lines)
    finally:
        await plugin.stop()


async def test_rps_win_emits_minigame_and_funken_events(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(irc_server, fake_client)
    plugin.rps._choice_provider = lambda: "scissors"  # deterministic: viewer's "rock" always wins
    try:
        await irc_server.send_privmsg(text="!rps stein", user_id="5", login="viewer5")
        ended = await _wait_event(fake_client, "stream.minigame_ended")
        assert ended["result"]["outcome"] == "win"
        assert ended["result"]["viewer_choice"] == "rock"
        awarded = await _wait_event(fake_client, "stream.funken_awarded")
        assert awarded["viewer_id"] == "5"
        assert awarded["reason"] == "rps_win"
        assert awarded["delta"] > 0
        await asyncio.sleep(0.1)
        # German reply: "stein" was the input alias.
        assert any("gewinnst" in line for line in irc_server.received_lines)
    finally:
        await plugin.stop()


async def test_rps_per_viewer_cooldown_blocks_a_second_round(
    irc_server: FakeIrcServer, fake_client: FakeClient
) -> None:
    api, plugin = await _plugin(
        irc_server, fake_client, rps_cooldown_s=60.0, rate_limit_min_gap_s=0.0
    )
    plugin.rps._choice_provider = lambda: "scissors"
    try:
        await irc_server.send_privmsg(text="!rps rock", user_id="6", login="viewer6")
        await _wait_event(fake_client, "stream.minigame_ended")
        before = len([e for e in fake_client.events if e[0] == "stream.minigame_started"])
        await irc_server.send_privmsg(text="!rps rock", user_id="6", login="viewer6")
        await asyncio.sleep(0.2)
        after = len([e for e in fake_client.events if e[0] == "stream.minigame_started"])
        assert after == before  # the second round never started - still on cooldown
        assert any("Wait" in line for line in irc_server.received_lines)
    finally:
        await plugin.stop()
