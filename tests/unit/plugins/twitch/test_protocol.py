"""IRC tag/command parsing (`nox_plugin_twitch.protocol`) - pure functions, no sockets."""

from __future__ import annotations

from nox_plugin_twitch.protocol import parse_chat_tags, parse_line, unescape_tag_value


def test_parses_ping_with_trailing_param() -> None:
    msg = parse_line("PING :tmi.twitch.tv")
    assert msg.command == "PING"
    assert msg.params == ["tmi.twitch.tv"]


def test_parses_join_without_trailing_param() -> None:
    msg = parse_line(":noxbot!noxbot@noxbot.tmi.twitch.tv JOIN #testchannel")
    assert msg.command == "JOIN"
    assert msg.params == ["#testchannel"]
    assert msg.prefix == "noxbot!noxbot@noxbot.tmi.twitch.tv"
    assert msg.nick == "noxbot"


def test_parses_privmsg_with_tags_and_display_name() -> None:
    line = (
        "@badge-info=;badges=broadcaster/1;color=;display-name=Viewer1;mod=0;subscriber=1;"
        "user-id=12345 :viewer1!viewer1@viewer1.tmi.twitch.tv PRIVMSG #testchannel :Hello world"
    )
    msg = parse_line(line)
    assert msg.command == "PRIVMSG"
    assert msg.params == ["#testchannel", "Hello world"]
    assert msg.tags["display-name"] == "Viewer1"
    assert msg.tags["user-id"] == "12345"
    assert msg.nick == "viewer1"


def test_parse_chat_tags_extracts_mod_subscriber_and_badges() -> None:
    tags = {
        "display-name": "Viewer1",
        "user-id": "12345",
        "mod": "1",
        "subscriber": "0",
        "badges": "moderator/1,premium/1",
    }
    chat_tags = parse_chat_tags(tags)
    assert chat_tags.display_name == "Viewer1"
    assert chat_tags.user_id == "12345"
    assert chat_tags.mod is True
    assert chat_tags.subscriber is False
    assert chat_tags.badges == ["moderator/1", "premium/1"]


def test_parse_chat_tags_defaults_when_missing() -> None:
    chat_tags = parse_chat_tags({})
    assert chat_tags == parse_chat_tags({})
    assert chat_tags.mod is False
    assert chat_tags.subscriber is False
    assert chat_tags.badges == []


def test_unescape_tag_value_handles_ircv3_escapes() -> None:
    assert unescape_tag_value("hello\\sworld") == "hello world"
    assert unescape_tag_value("a\\:b") == "a;b"
    assert unescape_tag_value("back\\\\slash") == "back\\slash"
    assert unescape_tag_value("no_escapes") == "no_escapes"


def test_parses_command_text_starting_with_bang() -> None:
    line = "@user-id=1 :viewer1!viewer1@viewer1.tmi.twitch.tv PRIVMSG #testchannel :!rps stein"
    msg = parse_line(line)
    text = msg.params[-1]
    assert text == "!rps stein"
    command, *args = text[1:].split()
    assert command == "rps"
    assert args == ["stein"]
