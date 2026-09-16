"""Hard-prohibition matching around `nox.security.hardlist.HARD_PROHIBITIONS` (Security Model §4,
ADR-007).

The constant lives in `hardlist.py`; this module adds glob-aware matching and the "config may only
add, never remove" validation. Nothing here can be overridden by profiles, grants, plugins or
an LLM.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

from nox.security.hardlist import HARD_PROHIBITIONS

_GLOB_CHARS = ("*", "?", "[")


class HardProhibitionRemovedError(ValueError):
    """A configuration or profile tried to drop or override a hard prohibition."""


def _normalise(value: str) -> str:
    return value.strip().lower()


def has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_CHARS)


def matching_hard_prohibition(tool_or_action: str) -> str | None:
    """Return the prohibited entry that `tool_or_action` names or (as a glob) covers, else None.

    `is_hard_prohibited("game.input.send")` and `is_hard_prohibited("game.input.*")` are both True;
    the bare wildcard "*" never counts as targeting a prohibition.
    """
    key = _normalise(tool_or_action)
    if not key or key == "*":
        return None
    if key in HARD_PROHIBITIONS:
        return key
    if has_glob(key):
        for entry in sorted(HARD_PROHIBITIONS):
            if fnmatchcase(entry, key):
                return entry
    return None


def is_hard_prohibited(tool_or_action: str) -> bool:
    """True when the name (or glob) is on the immutable deny list."""
    return matching_hard_prohibition(tool_or_action) is not None


def hard_prohibition_for(tool: str, action: str = "") -> str | None:
    """Check `tool`, `tool.action` and `action` (tool ids arrive in both naming conventions)."""
    candidates = [tool]
    if action:
        candidates.append(f"{tool}.{action}")
        candidates.append(action)
    for candidate in candidates:
        hit = matching_hard_prohibition(candidate)
        if hit is not None:
            return hit
    return None


def effective_hard_prohibitions(configured: Iterable[str]) -> frozenset[str]:
    """Union of the constant and the configured list; raises when the config drops an entry."""
    configured_set = frozenset(_normalise(entry) for entry in configured if entry.strip())
    missing = HARD_PROHIBITIONS.difference(configured_set)
    if missing:
        raise HardProhibitionRemovedError(
            "security.hard_prohibitions may only add entries; missing: "
            + ", ".join(sorted(missing))
        )
    return HARD_PROHIBITIONS.union(configured_set)
