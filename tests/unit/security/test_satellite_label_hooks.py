"""`DefaultPermissionEngine.add_satellite_label`: how a voice satellite is named in a question."""

from __future__ import annotations

import pytest

from nox.core.hooks import DuplicateHookError
from nox.security.permissions import DefaultPermissionEngine

PI = "a" * 32
OTHER = "b" * 32
VOICE = f"voice:node:{PI}#c0ffee"


def test_without_a_provider_a_satellite_is_named_by_the_start_of_its_id(
    engine: DefaultPermissionEngine,
) -> None:
    assert engine._satellite_of(VOICE) == PI[:8]
    assert engine._satellite_of("chat:dashboard:1#c0ffee") == ""  # not a satellite at all


def test_the_first_provider_that_knows_the_device_names_it(
    engine: DefaultPermissionEngine,
) -> None:
    asked: list[str] = []

    def unknown(_device: str) -> str:
        asked.append("unknown")
        return ""

    def nodes(device: str) -> str:
        asked.append("nodes")
        return "Living room Pi" if device == PI else ""

    def other(_device: str) -> str:
        asked.append("other")
        return "Somebody else"

    for name, provider in (("unknown", unknown), ("nodes", nodes), ("other", other)):
        engine.add_satellite_label(name, provider)
    assert engine._satellite_of(VOICE) == "Living room Pi"
    assert asked == ["unknown", "nodes"]
    assert engine._satellite_of(f"voice:node:{OTHER}#x") == "Somebody else"


def test_a_failing_provider_leaves_the_device_unnamed_not_the_question_unasked(
    engine: DefaultPermissionEngine,
) -> None:
    engine.add_satellite_label("broken", lambda _device: 1 / 0)  # type: ignore[arg-type,return-value]
    engine.add_satellite_label("nodes", lambda _device: "Living room Pi")
    assert engine._satellite_of(VOICE) == PI[:8]


def test_a_second_provider_under_the_same_name_is_refused(
    engine: DefaultPermissionEngine,
) -> None:
    engine.add_satellite_label("nodes", lambda _device: "Living room Pi")
    with pytest.raises(DuplicateHookError):
        engine.add_satellite_label("nodes", lambda _device: "Kitchen Pi")
    assert engine._satellite_of(VOICE) == "Living room Pi"
