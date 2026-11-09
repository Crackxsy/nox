"""Every write to `presets.*` needs the security PIN when one is set (C9 review, owner decision):
presets start registered programs and, marked `from_satellites`, can be started at a voice
satellite. No "does this widen anything" diff - any key, any value, removals included."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from nox.core.config import NoxConfig
from nox.security.gate import PinRequiredError
from nox.settings.editor import ConfigEditor
from tests.unit.fakes import FakeBus
from tests.unit.settings.conftest import FakeAudit
from tests.unit.settings.test_config_editor import file_exists

PIN = "2468"
PRESET = {
    "id": "cosy",
    "name": "Cosy",
    "triggers": {"phrases": ["kuschelabend"]},
    "steps": [{"kind": "scene", "entity_id": "scene.living_room"}],
}
ACTION = {"id": "lamp", "name": "Lamp", "command": [sys.executable, "--version"]}


class Gate:
    """A PIN is set; only `PIN` passes."""

    def __init__(self) -> None:
        self.asked: list[str | None] = []

    def is_required(self) -> bool:
        return True

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        self.asked.append(pin)
        if pin != PIN:
            raise PinRequiredError(action, "the security PIN is needed")


def _editor(config: NoxConfig, defaults: Path, user: Path, gate: Gate) -> ConfigEditor:
    return ConfigEditor(
        config=config,
        defaults_path=defaults,
        user_config_path=user,
        audit=FakeAudit(),
        bus=FakeBus(),
        gate=gate,
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("presets.enabled", False),
        ("presets.enabled", True),  # the same value as now: still a write
        ("presets.actions", [ACTION]),
        ("presets.actions", []),  # a removal
        ("presets.items", [PRESET]),
        ("presets.items", [{**PRESET, "from_satellites": True}]),
        ("presets.items", []),  # a removal
    ],
)
async def test_every_presets_write_needs_the_pin(
    config: NoxConfig, defaults_path: Path, user_config: Path, path: str, value: Any
) -> None:
    gate = Gate()
    editor = _editor(config, defaults_path, user_config, gate)
    for wrong in (None, "0000"):
        with pytest.raises(PermissionError):
            await editor.apply({path: value}, by="dashboard", pin=wrong)
    assert not file_exists(user_config)  # nothing was written
    result = await editor.apply({path: value}, by="dashboard", pin=PIN)
    assert result["ok"] is True and path in result["restart_required"] + result["applied"]


async def test_an_invalid_candidate_is_refused_with_the_pin_too(
    config: NoxConfig, defaults_path: Path, user_config: Path
) -> None:
    gate = Gate()
    editor = _editor(config, defaults_path, user_config, gate)
    coerced = [{**PRESET, "from_satellites": "true"}]  # never coerced to True
    with pytest.raises(PermissionError):
        await editor.apply({"presets.items": coerced}, by="dashboard", pin=None)
    result = await editor.apply({"presets.items": coerced}, by="dashboard", pin=PIN)
    assert result["ok"] is False and "presets.items" in result["errors"]
    assert not file_exists(user_config)


async def test_other_settings_still_need_no_pin(
    config: NoxConfig, defaults_path: Path, user_config: Path
) -> None:
    gate = Gate()
    editor = _editor(config, defaults_path, user_config, gate)
    result = await editor.apply({"voice.stt.wake_word": "Luna"}, by="dashboard", pin=None)
    assert result["ok"] is True and gate.asked == []
