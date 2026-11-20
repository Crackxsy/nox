"""`ToolRegistry.add_availability_probe`: extensions say why a routed tool cannot be used now."""

from __future__ import annotations

import pytest

from nox.capabilities.model import CapabilityState
from nox.core.hooks import DuplicateHookError
from nox.tools.registry import ToolRegistry

GONE = (CapabilityState.UNAVAILABLE, "no paired device with this is online")
OFF = (CapabilityState.UNAVAILABLE, "switched off")


def test_without_a_probe_every_tool_is_available() -> None:
    assert ToolRegistry().availability("notify.show") is None


def test_the_first_probe_with_an_answer_wins_and_the_rest_are_not_asked() -> None:
    tools = ToolRegistry()
    asked: list[str] = []

    def quiet(_tool: str) -> None:
        asked.append("quiet")

    def nodes(tool: str) -> tuple[CapabilityState, str] | None:
        asked.append("nodes")
        return GONE if tool == "notify.show" else None

    def other(_tool: str) -> tuple[CapabilityState, str]:
        asked.append("other")
        return OFF

    for name, probe in (("quiet", quiet), ("nodes", nodes), ("other", other)):
        tools.add_availability_probe(name, probe)
    assert tools.availability("notify.show") == GONE
    assert asked == ["quiet", "nodes"]
    asked.clear()
    assert tools.availability("file.read") == OFF  # `nodes` has nothing to say, `other` has
    assert asked == ["quiet", "nodes", "other"]


def test_a_second_probe_under_the_same_name_is_refused() -> None:
    tools = ToolRegistry()
    tools.add_availability_probe("nodes", lambda _tool: GONE)
    with pytest.raises(DuplicateHookError):
        tools.add_availability_probe("nodes", lambda _tool: OFF)
    assert tools.availability("anything") == GONE
