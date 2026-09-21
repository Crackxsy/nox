"""Twitch IRC wire parsing (Stream Bot).

Pure functions/dataclasses only - no sockets here. Twitch's IRC server speaks a subset of
RFC1459/IRCv3: an optional leading `@tag=value;...` block, an optional `:prefix`, a command, and
space-separated params where the last one may start with `:` and contain spaces (the "trailing"
parameter). See https://dev.twitch.tv/docs/irc/ (tags, capabilities, message format).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# IRCv3 tag value escaping (https://ircv3.net/specs/extensions/message-tags): a literal `\` in a
# tag value is escaped as one of these two-character sequences.
_TAG_UNESCAPES = {
    "\\:": ";",
    "\\s": " ",
    "\\\\": "\\",
    "\\r": "\r",
    "\\n": "\n",
}


def unescape_tag_value(value: str) -> str:
    if "\\" not in value:
        return value
    out: list[str] = []
    i = 0
    while i < len(value):
        pair = value[i : i + 2]
        if pair in _TAG_UNESCAPES:
            out.append(_TAG_UNESCAPES[pair])
            i += 2
        elif value[i] == "\\" and i + 1 == len(value):
            i += 1  # a dangling trailing backslash is dropped, never half-consumed
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class IrcMessage:
    tags: dict[str, str] = field(default_factory=dict)
    prefix: str = ""
    command: str = ""
    params: list[str] = field(default_factory=list)

    @property
    def nick(self) -> str:
        """The sending user's login, from a `nick!user@host` prefix."""
        return self.prefix.split("!", 1)[0]


def parse_line(raw: str) -> IrcMessage:
    """Parse one IRC line (no trailing `\\r\\n`, callers strip that first)."""
    line = raw.strip("\r\n")
    tags: dict[str, str] = {}
    if line.startswith("@"):
        tag_part, _, line = line.partition(" ")
        for kv in tag_part[1:].split(";"):
            if not kv:
                continue
            key, _, value = kv.partition("=")
            tags[key] = unescape_tag_value(value)
    prefix = ""
    if line.startswith(":"):
        prefix_part, _, line = line.partition(" ")
        prefix = prefix_part[1:]
    if " :" in line:
        head, _, trailing = line.partition(" :")
        params = [p for p in head.split(" ") if p]
        params.append(trailing)
    elif line.startswith(":"):
        params = [line[1:]]
    else:
        params = [p for p in line.split(" ") if p]
    command = params[0] if params else ""
    rest = params[1:] if params else []
    return IrcMessage(tags=tags, prefix=prefix, command=command, params=rest)


@dataclass(frozen=True, slots=True)
class ChatTags:
    display_name: str = ""
    user_id: str = ""
    mod: bool = False
    subscriber: bool = False
    badges: list[str] = field(default_factory=list)


def parse_chat_tags(tags: Mapping[str, Any]) -> ChatTags:
    badges_raw = str(tags.get("badges", ""))
    badges = [b for b in badges_raw.split(",") if b]
    return ChatTags(
        display_name=str(tags.get("display-name", "")),
        user_id=str(tags.get("user-id", "")),
        mod=str(tags.get("mod", "0")) == "1",
        subscriber=str(tags.get("subscriber", "0")) == "1",
        badges=badges,
    )
