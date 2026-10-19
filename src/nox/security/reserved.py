"""Names Nox never lets a foreign or self-built tool take, and text it never lets one write.

Two kinds of capability arrive from outside the source tree: tools of other people's MCP servers
(`nox.mcp`) and plugins Nox builds for itself (`nox.skills`). Both bring names and descriptions
that nobody at Nox reviewed. This module holds the checks both sides share, so they cannot drift
apart:

* **Reserved namespaces** - `security`, `home`, `desktop`, `game`, `permission` and `mcp` belong to
  the core. A foreign tool named `home_unlock` or `security.disable` must not sit next to the real
  ones in the model's tool list, where it would read like one of them.
* **Hard prohibitions** in every spelling a tool name can have on the wire (`game_input_send`,
  `game-input-send`, `Game.Input.Send`).
* **The home boundary** (`nox.home.boundary`): locks, alarms, valves and garage doors are not
  reachable through a foreign tool either - not by its name and not by an argument that names such
  an entity.
* **Untrusted text**: a description is data. It is cut short, stripped of control and
  direction-override characters, and never placed where an instruction would be.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Final

from nox.home.boundary import FORBIDDEN_COVER_DEVICE_CLASSES, FORBIDDEN_DOMAINS
from nox.security.prohibitions import matching_hard_prohibition

__all__ = [
    "RESERVED_NAMESPACES",
    "forbidden_home_reference",
    "name_refusal",
    "reserved_namespace_of",
    "untrusted_text",
]

#: First segments only the core may register tools under.
RESERVED_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"security", "home", "desktop", "game", "permission", "mcp"}
)

#: Words in a foreign tool's *name* that point at the home boundary. Matched as whole tokens.
_BOUNDARY_WORDS: Final[frozenset[str]] = frozenset(
    {
        "lock",
        "unlock",
        "locks",
        "alarm",
        "disarm",
        "arm",
        "garage",
        "gate",
        "valve",
        "valves",
        "door",
        "doors",
        *FORBIDDEN_DOMAINS,
        *FORBIDDEN_COVER_DEVICE_CLASSES,
    }
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
#: `lock.front_door`, `alarm_control_panel.home`, `valve.main` - an entity id in a forbidden domain.
_FORBIDDEN_ENTITY = re.compile(
    r"\b(?:" + "|".join(sorted(re.escape(d) for d in FORBIDDEN_DOMAINS)) + r")\.[a-z0-9_]+",
    re.IGNORECASE,
)
#: Keys under which a Home-Assistant-shaped call names its domain or device class.
_DOMAIN_KEYS: Final[frozenset[str]] = frozenset({"domain", "device_class", "entity_domain"})

#: Characters that change how text renders without being visible: bidi overrides, zero-width.
_INVISIBLE: Final[frozenset[str]] = frozenset(
    {
        "​",
        "‌",
        "‍",
        "⁠",
        "﻿",
        "‪",
        "‫",
        "‬",
        "‭",
        "‮",
        "⁦",
        "⁧",
        "⁨",
        "⁩",
    }
)
#: Cap on how deep `forbidden_home_reference` follows nested arguments.
_MAX_DEPTH = 8


def _tokens(name: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(name.lower()) if token]


def reserved_namespace_of(name: str) -> str | None:
    """The reserved namespace `name` would land in, judged by its first token, else None."""
    tokens = _tokens(name)
    if tokens and tokens[0] in RESERVED_NAMESPACES:
        return tokens[0]
    return None


def _hard_prohibition_in(name: str) -> str | None:
    """A hard prohibition named by `name` in any spelling, also as a dotted suffix of it."""
    tokens = _tokens(name)
    for start in range(len(tokens)):
        hit = matching_hard_prohibition(".".join(tokens[start:]))
        if hit is not None:
            return hit
    return None


def name_refusal(name: str) -> str | None:
    """Why a foreign or self-built tool may not carry `name`, or None when it may."""
    if not _tokens(name):
        return "the name is empty"
    hard = _hard_prohibition_in(name)
    if hard is not None:
        return f"names the hard prohibition {hard!r}"
    reserved = reserved_namespace_of(name)
    if reserved is not None:
        return f"uses the reserved namespace {reserved!r}"
    boundary = sorted(set(_tokens(name)) & _BOUNDARY_WORDS)
    if boundary:
        return f"points at the home boundary ({boundary[0]!r}: locks, alarms, valves, garage doors)"
    return None


def forbidden_home_reference(arguments: Any, *, _depth: int = 0) -> str | None:
    """The first argument value that names a locked-away entity or domain, else None.

    Looks at every string in `arguments`, nested lists and mappings included: an entity id such as
    `lock.front_door`, or a `domain`/`device_class` key whose value is a forbidden domain or a
    garage/gate/door cover.
    """
    if _depth > _MAX_DEPTH:
        return "arguments nested too deeply to check"
    if isinstance(arguments, str):
        match = _FORBIDDEN_ENTITY.search(arguments)
        return match.group(0).lower() if match else None
    if isinstance(arguments, Mapping):
        for key, value in arguments.items():
            if (
                isinstance(key, str)
                and key.lower() in _DOMAIN_KEYS
                and isinstance(value, str)
                and value.strip().lower() in FORBIDDEN_DOMAINS | FORBIDDEN_COVER_DEVICE_CLASSES
            ):
                return f"{key}={value.strip().lower()}"
            found = forbidden_home_reference(value, _depth=_depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(arguments, Iterable) and not isinstance(arguments, bytes | bytearray):
        for item in arguments:
            found = forbidden_home_reference(item, _depth=_depth + 1)
            if found is not None:
                return found
    return None


def untrusted_text(text: object, limit: int) -> str:
    """`text` made safe to show a model as data: printable, one line, at most `limit` characters."""
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    kept = []
    for char in raw:
        if char in _INVISIBLE:
            continue
        category = unicodedata.category(char)
        kept.append(" " if category.startswith("C") or category in ("Zl", "Zp") else char)
    flat = " ".join("".join(kept).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)].rstrip() + "…"
