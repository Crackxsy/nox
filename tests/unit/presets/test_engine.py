"""Matching a sentence to a preset, and running one.

The matching half mirrors `tests/unit/home/test_intent.py`: the cases that must *not* match are
the load-bearing ones, because a matcher that guesses dims the lights while someone is explaining
what a preset is.

The running half pins down one promise in particular - a preset is not a transaction. When the
lamp is unplugged the mouse still changes its sensitivity, and the run says plainly which step
failed instead of reporting the whole preset as broken.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from nox.core.config.presets import PresetsConfig
from nox.presets.engine import PresetRunner, match_phrase, normalise

CONFIG = PresetsConfig.model_validate(
    {
        "actions": [
            {"id": "dpi_low", "name": "DPI 800", "command": ["C:/Tools/ahk.exe", "dpi.ahk"]}
        ],
        "items": [
            {
                "id": "gaming",
                "name": "Gaming",
                "triggers": {"phrases": ["gaming mode", "zocken"]},
                "steps": [
                    {"kind": "light", "entity_ids": ["light.desk"], "brightness_pct": 30},
                    {"kind": "run", "action": "dpi_low"},
                    {"kind": "mode", "mode": "companion"},
                    {"kind": "say", "text": "Viel Erfolg."},
                ],
            },
            {
                "id": "gaming_off",
                "name": "Gaming aus",
                "triggers": {"phrases": ["gaming mode aus"]},
                "steps": [{"kind": "light", "entity_ids": ["light.desk"], "on": True}],
            },
            {
                "id": "off",
                "name": "Aus",
                "enabled": False,
                "triggers": {"phrases": ["alles aus"]},
                "steps": [{"kind": "switch", "entity_ids": ["switch.a"], "on": False}],
            },
        ],
    }
)


@dataclass
class Outcome:
    ok: bool
    error: str = ""
    data: dict[str, Any] | None = None


@dataclass
class Recorder:
    """Stands in for the core: records what was asked of it, and can be told to fail."""

    failing_tools: set[str] = field(default_factory=set)
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Outcome:
        self.tools.append((name, arguments))
        if name in self.failing_tools:
            return Outcome(ok=False, error=f"{name} is unreachable")
        return Outcome(ok=True, data={"ok": True})

    async def set_mode(self, mode: str) -> None:
        self.modes.append(mode)

    async def say(self, text: str) -> None:
        self.said.append(text)

    def runner(self, config: PresetsConfig = CONFIG) -> PresetRunner:
        return PresetRunner(
            settings=lambda: config,
            call_tool=self.call_tool,
            set_mode=self.set_mode,
            say=self.say,
        )


# ---- matching --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("gaming mode", "gaming"),
        ("Gaming Mode bitte", "gaming"),
        ("gaming-mode!", "gaming"),
        ("lass uns zocken", "gaming"),
        # The longer phrase wins, so "off" can live next to "on".
        ("gaming mode aus", "gaming_off"),
    ],
)
def test_a_registered_phrase_reaches_its_preset(sentence: str, expected: str) -> None:
    hit = match_phrase(sentence, CONFIG.items)

    assert hit is not None
    assert hit.id == expected


@pytest.mark.parametrize(
    "sentence",
    [
        "was bedeutet gaming mode",
        "kannst du mir erklaeren was gaming mode macht?",
        "ich wollte dir nur erzaehlen dass gaming mode ein begriff ist",
        # No word boundary: this is one word, not the phrase.
        "ich rede ueber gamingmode",
        "wie spaet ist es",
        "",
    ],
)
def test_talking_about_a_preset_does_not_activate_it(sentence: str) -> None:
    assert match_phrase(sentence, CONFIG.items) is None


def test_a_disabled_preset_never_matches() -> None:
    assert match_phrase("alles aus", CONFIG.items) is None


def test_normalise_folds_case_accents_and_punctuation() -> None:
    assert normalise("Gaming-Mode!") == "gaming mode"
    assert normalise("CAFE\u0301") == "cafe"


# ---- running ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_step_runs_in_order() -> None:
    recorder = Recorder()

    run = await recorder.runner().activate("gaming", trigger="phrase")

    assert run.ok
    assert [step.kind for step in run.steps] == ["light", "run", "mode", "say"]
    assert recorder.modes == ["companion"]
    assert recorder.said == ["Viel Erfolg."]


@pytest.mark.asyncio
async def test_a_program_start_goes_through_the_tool_executor() -> None:
    """Not around it: that is what gives it a permission check and an audit entry."""
    recorder = Recorder()

    await recorder.runner().activate("gaming")

    assert ("presets.run_action", {"action": "dpi_low"}) in recorder.tools


@pytest.mark.asyncio
async def test_a_failed_step_does_not_stop_the_ones_after_it() -> None:
    """The unplugged lamp must not keep the mouse from changing its sensitivity."""
    recorder = Recorder(failing_tools={"home.light"})

    run = await recorder.runner().activate("gaming")

    assert not run.ok
    assert [step.ok for step in run.steps] == [False, True, True, True]
    assert run.failures[0].kind == "light"
    assert recorder.modes == ["companion"]
    assert ("presets.run_action", {"action": "dpi_low"}) in recorder.tools


@pytest.mark.asyncio
async def test_a_failed_program_is_announced() -> None:
    recorder = Recorder(failing_tools={"presets.run_action"})

    run = await recorder.runner().activate("gaming")

    assert not run.ok
    assert any("presets.run_action is unreachable" in line for line in recorder.said)


@pytest.mark.asyncio
async def test_light_arguments_reach_the_tool_unchanged() -> None:
    recorder = Recorder()

    await recorder.runner().activate("gaming")

    name, arguments = recorder.tools[0]
    assert name == "home.light"
    assert arguments == {"entity_ids": ["light.desk"], "brightness_pct": 30}


@pytest.mark.asyncio
async def test_an_unknown_preset_raises() -> None:
    with pytest.raises(KeyError):
        await Recorder().runner().activate("nothing-like-this")


@pytest.mark.asyncio
async def test_the_same_preset_does_not_interleave_with_itself() -> None:
    """Saying the phrase twice quickly must run the steps twice, not braid them together."""
    recorder = Recorder()
    runner = recorder.runner()

    await asyncio.gather(runner.activate("gaming"), runner.activate("gaming"))

    called = [name for name, _ in recorder.tools]
    assert called == ["home.light", "presets.run_action"] * 2


@pytest.mark.asyncio
async def test_a_say_step_can_be_redirected_for_one_run() -> None:
    """A preset started by voice answers through the turn instead of speaking over it."""
    recorder = Recorder()
    collected: list[str] = []

    async def collect(line: str) -> None:
        collected.append(line)

    await recorder.runner().activate("gaming", trigger="phrase", say=collect)

    assert collected == ["Viel Erfolg."]
    assert recorder.said == []
