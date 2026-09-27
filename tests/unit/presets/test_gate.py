"""What Nox answers when a spoken phrase activated a preset.

The rule under test is honesty over tidiness: a preset that half worked must not sound like it
fully worked. The other half is that the user's own words win - a preset with a `say` step says
exactly that, without a generic confirmation stapled to it.
"""

from __future__ import annotations

from typing import Any

import pytest

from nox.core.config.presets import PresetsConfig
from nox.presets.engine import PresetRun, PresetRunner, StepResult
from nox.presets.gate import VoicePresetGate, compose_answer

CONFIG = PresetsConfig.model_validate(
    {
        "items": [
            {
                "id": "gaming",
                "name": "Gaming",
                "triggers": {"phrases": ["gaming mode"]},
                "steps": [{"kind": "light", "entity_ids": ["light.desk"], "on": True}],
            }
        ]
    }
)


def run_with(*steps: StepResult) -> PresetRun:
    return PresetRun(preset_id="gaming", name="Gaming", trigger="phrase", steps=list(steps))


def ok_step(kind: str = "light") -> StepResult:
    return StepResult(index=1, kind=kind, ok=True)


def failed_step(reason: str, kind: str = "light") -> StepResult:
    return StepResult(index=1, kind=kind, ok=False, error=reason)


async def _unused_tool(_name: str, _arguments: dict[str, Any]) -> Any:
    raise AssertionError("no tool should be called")


async def _unused_mode(_mode: str) -> None:
    raise AssertionError("the mode should not change")


async def _unused_say(_text: str) -> None:
    raise AssertionError("nothing should be spoken")


def test_a_silent_preset_is_confirmed_by_name() -> None:
    assert compose_answer(run_with(ok_step()), [], "de") == "Gaming ist an."
    assert compose_answer(run_with(ok_step()), [], "en") == "Gaming is on."


def test_the_users_own_words_replace_the_confirmation() -> None:
    answer = compose_answer(run_with(ok_step()), ["Viel Erfolg."], "de")

    assert answer == "Viel Erfolg."


def test_one_failure_is_named() -> None:
    answer = compose_answer(run_with(failed_step("die Lampe antwortet nicht")), [], "de")

    assert "Gaming ist an" in answer
    assert "die Lampe antwortet nicht" in answer


def test_several_failures_are_counted() -> None:
    steps = (failed_step("Lampe weg"), failed_step("Maus weg", kind="run"))

    answer = compose_answer(run_with(*steps), [], "de")

    assert "2 Schritte" in answer
    assert "Lampe weg" in answer


def test_a_failure_is_not_said_twice() -> None:
    """The step already announced itself; the summary must not repeat it word for word."""
    answer = compose_answer(run_with(failed_step("Maus weg", kind="run")), ["Maus weg"], "de")

    assert answer.count("Maus weg") == 1


@pytest.mark.asyncio
async def test_a_sentence_without_a_phrase_is_left_alone() -> None:
    """`None` is what tells the orchestrator to carry on to the fast path and the model."""
    runner = PresetRunner(
        settings=lambda: CONFIG,
        call_tool=_unused_tool,
        set_mode=_unused_mode,
        say=_unused_say,
    )

    assert await VoicePresetGate(runner).handle("wie spaet ist es", "de") is None
