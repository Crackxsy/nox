"""What the preset configuration accepts, and - more importantly - what it refuses.

The refusals carry the weight here. A preset section is the list of things a spoken phrase is
allowed to set in motion, so every entry that would be surprising later has to be a loud error
now: a step pointing at an action nobody registered, a program named without a path, a phrase so
short that ordinary speech would trip it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from nox.core.config.presets import PresetActionConfig, PresetsConfig

AHK = "C:/Tools/AutoHotkey.exe"


def section(**overrides: Any) -> dict[str, Any]:
    """A valid section, so each test can change exactly the one thing it is about."""
    payload: dict[str, Any] = {
        "actions": [{"id": "dpi_low", "name": "DPI 800", "command": [AHK, "dpi800.ahk"]}],
        "items": [
            {
                "id": "gaming",
                "name": "Gaming",
                "triggers": {"phrases": ["gaming mode"]},
                "steps": [
                    {"kind": "light", "entity_ids": ["light.desk"], "brightness_pct": 30},
                    {"kind": "run", "action": "dpi_low"},
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def preset_with(triggers: dict[str, Any]) -> dict[str, Any]:
    return {**section()["items"][0], "triggers": triggers}


def test_a_complete_section_validates() -> None:
    config = PresetsConfig.model_validate(section())

    assert [preset.id for preset in config.items] == ["gaming"]
    assert [step.kind for step in config.items[0].steps] == ["light", "run"]


def test_phrases_are_lowercased_and_trimmed() -> None:
    config = PresetsConfig.model_validate(
        section(items=[preset_with({"phrases": ["  Gaming MODE "]})])
    )

    assert config.items[0].triggers.phrases == ["gaming mode"]


def test_weekdays_are_normalised_to_three_letters() -> None:
    config = PresetsConfig.model_validate(
        section(items=[preset_with({"at": "20:00", "days": ["Monday", "FRI"]})])
    )

    assert config.items[0].triggers.days == ["mon", "fri"]


def test_a_step_may_not_run_an_unregistered_action() -> None:
    with pytest.raises(ValidationError, match="unknown action 'ghost'"):
        PresetsConfig.model_validate(
            section(
                actions=[],
                items=[{"id": "x", "name": "X", "steps": [{"kind": "run", "action": "ghost"}]}],
            )
        )


def test_a_program_needs_an_absolute_path() -> None:
    """A bare name would be looked up in PATH and could mean a different program every start."""
    with pytest.raises(ValidationError, match="absolute path"):
        PresetActionConfig.model_validate(
            {"id": "a", "name": "Notepad", "command": ["notepad.exe"]}
        )


@pytest.mark.parametrize("program", [AHK, "//server/share/tool.exe"])
def test_absolute_paths_are_accepted(program: str) -> None:
    action = PresetActionConfig.model_validate({"id": "a", "name": "A", "command": [program]})

    assert action.command[0] == program


def test_a_phrase_that_would_fire_on_ordinary_speech_is_refused() -> None:
    with pytest.raises(ValidationError, match="shorter than"):
        PresetsConfig.model_validate(section(items=[preset_with({"phrases": ["ja"]})]))


def test_two_presets_may_not_claim_the_same_phrase() -> None:
    """Otherwise which one runs would depend on the order they happen to be written in."""
    preset = section()["items"][0]

    with pytest.raises(ValidationError, match="duplicate entry in trigger phrases"):
        PresetsConfig.model_validate(
            section(items=[preset, {**preset, "id": "gaming2", "name": "Gaming 2"}])
        )


def test_duplicate_identifiers_are_refused() -> None:
    action = {"id": "dpi_low", "name": "DPI 800", "command": [AHK]}

    with pytest.raises(ValidationError, match="duplicate entry in actions"):
        PresetsConfig.model_validate(section(actions=[action, {**action, "name": "Again"}]))


def test_days_without_a_time_are_refused() -> None:
    with pytest.raises(ValidationError, match="days were given without a time"):
        PresetsConfig.model_validate(section(items=[preset_with({"days": ["mon"]})]))


@pytest.mark.parametrize("at", ["25:00", "7:00", "19:60", "abends"])
def test_an_impossible_time_is_refused(at: str) -> None:
    with pytest.raises(ValidationError):
        PresetsConfig.model_validate(section(items=[preset_with({"at": at})]))


def test_a_light_step_has_to_change_something() -> None:
    with pytest.raises(ValidationError, match="at least one of"):
        PresetsConfig.model_validate(
            section(
                actions=[],
                items=[
                    {
                        "id": "x",
                        "name": "X",
                        "steps": [{"kind": "light", "entity_ids": ["light.a"]}],
                    }
                ],
            )
        )


def test_an_empty_section_is_valid_and_does_nothing() -> None:
    """A fresh installation ships this: presets on, nothing registered, nothing runnable."""
    config = PresetsConfig.model_validate({})

    assert config.enabled
    assert config.items == []
    assert config.actions == []
