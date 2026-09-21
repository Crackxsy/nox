"""The two glob dialects Nox uses, in one place.

* **Value globs** (`value_matches`, `value_matches_any`) are case-insensitive `fnmatch`: `*`
  matches any run of characters, `/` and `.` included. Tool ids, actions, window titles, host
  names and permission-rule fields are matched this way.
* **Name globs** (`name_matches`, `name_matches_any`) are dotted and segment-aware: a pattern
  segment matches exactly one name segment, with `fnmatch` inside it, and `**` matches any number
  of segments. Event and IPC request names are matched this way, which is why `security.*` covers
  `security.kill` but not `security.permission.reply`.

Both dialects used to exist in four modules with three different behaviours and, in two of them,
with reversed argument orders, so a reader could not tell two call sites apart. Every argument
order here is `(subject, pattern)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

__all__ = [
    "name_matches",
    "name_matches_any",
    "value_matches",
    "value_matches_any",
]


def value_matches(value: str, pattern: str) -> bool:
    """Case-insensitive glob over a whole value; `"*"` matches everything, the empty string too."""
    if pattern == "*":
        return True
    return fnmatchcase(value.lower(), pattern.lower())


def value_matches_any(value: str, patterns: Sequence[str]) -> bool:
    return any(value_matches(value, p) for p in patterns)


def name_matches(name: str, pattern: str) -> bool:
    """Dotted glob: one pattern segment matches one name segment, `**` matches any number."""
    return _match_segments(pattern.split("."), name.split("."))


def name_matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(name_matches(name, p) for p in patterns)


def _match_segments(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match_segments(rest, parts[index:]) for index in range(len(parts) + 1))
    if not parts:
        return False
    return fnmatchcase(parts[0], head) and _match_segments(rest, parts[1:])
