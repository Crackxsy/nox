"""Hard prohibitions: constant matches defaults.yaml, glob matching, config may only add."""

from __future__ import annotations

import pytest
import yaml

from nox.security.hardlist import HARD_PROHIBITIONS
from nox.security.prohibitions import (
    HardProhibitionRemovedError,
    effective_hard_prohibitions,
    hard_prohibition_for,
    is_hard_prohibited,
)

from .conftest import DEFAULTS_YAML

EXPECTED = {
    "game.input.send",
    "game.memory.read",
    "game.process.inject",
    "anticheat.bypass",
    "stream.key.read",
    "stream.stop",
    "recording.delete",
    "security.core.modify_without_pin",
    "permission.self_elevate",
}


def test_constant_has_exactly_the_nine_entries_from_defaults() -> None:
    cfg = yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8"))
    assert HARD_PROHIBITIONS == EXPECTED
    assert set(cfg["security"]["hard_prohibitions"]) == EXPECTED
    assert isinstance(HARD_PROHIBITIONS, frozenset)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_entry_is_prohibited(name: str) -> None:
    assert is_hard_prohibited(name)
    assert is_hard_prohibited(name.upper())
    assert is_hard_prohibited(f"  {name} ")


def test_glob_argument_covering_an_entry_counts_as_prohibited() -> None:
    assert is_hard_prohibited("game.input.*")
    assert is_hard_prohibited("game.*")
    assert is_hard_prohibited("stream.?top")
    assert not is_hard_prohibited("*")  # the bare wildcard is not a targeted attempt
    assert not is_hard_prohibited("")


def test_unrelated_names_are_not_prohibited() -> None:
    for name in (
        "game.input",
        "game.input.observe",
        "stream.start",
        "recording.read",
        "memory.write",
    ):
        assert not is_hard_prohibited(name), name


def test_tool_action_combination_is_checked() -> None:
    assert hard_prohibition_for("game.input", "send") == "game.input.send"
    assert hard_prohibition_for("stream", "stop") == "stream.stop"
    assert hard_prohibition_for("permission", "self_elevate") == "permission.self_elevate"
    assert hard_prohibition_for("obs", "scene.switch") is None


def test_config_may_add_but_never_remove() -> None:
    extended = effective_hard_prohibitions([*EXPECTED, "plugin.evil"])
    assert "plugin.evil" in extended and EXPECTED <= extended
    with pytest.raises(HardProhibitionRemovedError, match="stream.stop"):
        effective_hard_prohibitions(sorted(EXPECTED - {"stream.stop"}))
    with pytest.raises(HardProhibitionRemovedError):
        effective_hard_prohibitions([])
