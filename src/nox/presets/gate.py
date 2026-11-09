"""The step between a spoken sentence and a preset.

`nox.core.orchestrator` asks this before it asks anything else. When a registered phrase is in the
sentence, the preset runs and this returns the sentence Nox should answer with; otherwise it
returns `None` and the turn carries on to the fast path and the model.

Everything a preset would have spoken is collected here rather than spoken directly. A preset
started by voice is already a conversation turn, and two voices talking over each other - the
preset's own `say` step and the answer to the turn - is the kind of detail that makes an assistant
feel broken.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from nox.core.logging import get_logger
from nox.presets.engine import PresetRun, PresetRunner
from nox.security.taint import CONVERSATION

log = get_logger(__name__)

__all__ = ["FROM_SATELLITE", "SATELLITE_AGENT", "VoicePresetGate", "compose_answer"]

#: Set while a preset runs because a node's voice satellite said its phrase (C9): its steps are
#: called as this agent - not `presets`, which the shipped profiles allow - in the node's
#: conversation, so the normal rules and that conversation's taint apply; mode steps refuse.
FROM_SATELLITE: ContextVar[bool] = ContextVar("nox_preset_from_satellite", default=False)
SATELLITE_AGENT = "presets.satellite"

_GERMAN = "de"

_CONFIRMATION = {
    "de": "{name} ist an.",
    "en": "{name} is on.",
}

_ONE_FAILED = {
    "de": "{name} ist an, aber ein Schritt hat nicht geklappt: {reason}",
    "en": "{name} is on, but one step did not work: {reason}",
}

_SEVERAL_FAILED = {
    "de": "{name} ist an, aber {count} Schritte haben nicht geklappt. Der erste: {reason}",
    "en": "{name} is on, but {count} steps did not work. The first: {reason}",
}


def _pick(table: dict[str, str], language: str) -> str:
    return table[_GERMAN] if language.lower().startswith(_GERMAN) else table["en"]


def compose_answer(run: PresetRun, spoken: list[str], language: str) -> str:
    """Turn a finished run into the one sentence the user hears.

    A preset that says something of its own says exactly that - the user wrote those words, and a
    generic confirmation after them would only be noise. Failures are always reported, because a
    preset that half worked and sounds like it fully worked is worse than one that plainly failed.
    """
    failures = run.failures
    if not failures:
        return " ".join(spoken) if spoken else _pick(_CONFIRMATION, language).format(name=run.name)

    reason = failures[0].error
    if len(failures) == 1:
        trouble = _pick(_ONE_FAILED, language).format(name=run.name, reason=reason)
    else:
        trouble = _pick(_SEVERAL_FAILED, language).format(
            name=run.name, count=len(failures), reason=reason
        )
    # Anything the preset said before it ran into trouble still belongs in the answer, but the
    # failure itself is already in `trouble` and must not be repeated.
    remaining = [line for line in spoken if line != reason]
    return " ".join([*remaining, trouble]) if remaining else trouble


@dataclass(slots=True)
class VoicePresetGate:
    """Implements `nox.core.orchestrator.PresetGate` on top of a `PresetRunner`."""

    runner: PresetRunner

    async def handle(self, text: str, language: str) -> str | None:
        preset = self.runner.match(text)
        if preset is None:
            return None

        spoken: list[str] = []

        async def collect(line: str) -> None:
            spoken.append(line)

        run = await self.runner.activate(preset.id, trigger="phrase", say=collect)
        log.info("presets.spoken", preset=preset.id, ok=run.ok)
        return compose_answer(run, spoken, language)

    async def handle_from_satellite(
        self, text: str, language: str, *, conversation: str
    ) -> str | None:
        """A phrase said at a node's voice satellite: only a preset the owner marked
        `from_satellites` runs - as an untrusted turn's, in that turn's conversation."""
        preset = self.runner.match(text)
        if preset is None or not getattr(preset, "from_satellites", False):
            return None
        spoken: list[str] = []

        async def collect(line: str) -> None:
            spoken.append(line)

        mark = FROM_SATELLITE.set(True)
        where = CONVERSATION.set(conversation)
        try:
            run = await self.runner.activate(preset.id, trigger="satellite", say=collect)
        finally:
            CONVERSATION.reset(where)
            FROM_SATELLITE.reset(mark)
        log.info("presets.spoken", preset=preset.id, ok=run.ok, from_satellite=True)
        return compose_answer(run, spoken, language)
