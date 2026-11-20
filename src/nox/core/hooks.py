"""`NamedHooks`: an extension point many extensions can join without overwriting each other.

A single callable slot on a core component ("the one provider of X") is silently replaced by
whoever installs last, and what wins depends on the installation order. A `NamedHooks` list keeps
every registration, calls them in the order they were registered, and refuses a second
registration under a name that is already taken - so a doubled installation fails where it
happens instead of showing up later as a line that appears twice.

It owns the bookkeeping only. What a list of hooks means - every hook adds a line, the first
non-empty answer wins, every hook must agree - is decided by the component that owns the list.
"""

from __future__ import annotations

from collections.abc import Iterator

__all__ = ["DuplicateHookError", "NamedHooks"]


class DuplicateHookError(ValueError):
    """A hook was registered under a name that an earlier registration already holds."""


class NamedHooks[H]:
    """Hooks in registration order, each under a name that can be taken only once."""

    def __init__(self, what: str) -> None:
        #: What the hooks are, for the error message ("fact source", "capability probe").
        self._what = what
        self._hooks: dict[str, H] = {}

    def add(self, name: str, hook: H) -> None:
        """Register `hook` after all earlier ones. A name that is taken already raises."""
        if name in self._hooks:
            raise DuplicateHookError(f"{self._what} {name!r} is already registered")
        self._hooks[name] = hook

    @property
    def names(self) -> tuple[str, ...]:
        """The registered names, in call order."""
        return tuple(self._hooks)

    def __iter__(self) -> Iterator[H]:
        return iter(list(self._hooks.values()))

    def __len__(self) -> int:
        return len(self._hooks)
