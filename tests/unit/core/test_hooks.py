"""`NamedHooks`: many extensions join one extension point, in order, each name once."""

from __future__ import annotations

import pytest

from nox.core.hooks import DuplicateHookError, NamedHooks


def test_hooks_come_back_in_the_order_they_were_registered() -> None:
    hooks: NamedHooks[str] = NamedHooks("greeting")
    for name in ("third", "first", "second"):
        hooks.add(name, name.upper())
    assert list(hooks) == ["THIRD", "FIRST", "SECOND"]
    assert hooks.names == ("third", "first", "second")
    assert len(hooks) == 3


def test_a_name_registers_once_and_the_first_registration_stays() -> None:
    hooks: NamedHooks[int] = NamedHooks("fact source")
    hooks.add("nodes", 1)
    with pytest.raises(DuplicateHookError, match="fact source 'nodes' is already registered"):
        hooks.add("nodes", 2)
    assert list(hooks) == [1]


def test_nothing_registered_is_falsy_and_empty() -> None:
    hooks: NamedHooks[int] = NamedHooks("probe")
    assert not hooks and list(hooks) == [] and hooks.names == ()


def test_a_hook_added_while_the_hooks_run_is_not_part_of_that_round() -> None:
    hooks: NamedHooks[str] = NamedHooks("probe")
    hooks.add("a", "a")
    seen: list[str] = []
    for hook in hooks:
        seen.append(hook)
        hooks.add("b", "b")
    assert seen == ["a"] and list(hooks) == ["a", "b"]
