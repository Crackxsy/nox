"""Running a preset: match a phrase to it, then walk its steps in order.

Two decisions shape this module.

A preset is not a transaction. When the lamp in the living room is unplugged, the mouse should
still change its sensitivity and the mode should still switch - so a failed step is recorded and
the remaining steps run anyway. The run reports every step, and the caller can see exactly which
part did not work instead of being told the whole preset failed.

Matching happens before any language model is asked. A spoken sentence is normalised and searched
for the registered phrases as whole words, which costs a few microseconds; only when nothing
matches does the sentence continue on its ordinary path. The longest matching phrase wins, so
"gaming mode aus" can be its own preset next to "gaming mode".
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from nox.core.config.presets import (
    ClimateStep,
    LightStep,
    ModeStep,
    PresetConfig,
    PresetsConfig,
    PresetStep,
    RunStep,
    SayStep,
    SceneStep,
    SwitchStep,
    WaitStep,
)
from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "PresetRun",
    "PresetRunner",
    "StepResult",
    "ToolOutcome",
    "match_phrase",
    "normalise",
]

#: Everything that is not a letter, a digit or a space is dropped before matching, so
#: "Gaming-Mode!" and "gaming mode" are the same phrase.
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

#: Longest sentence still treated as a command rather than as talk. A phrase plus a couple of
#: filler words ("gaming mode bitte an") fits; a sentence that explains something does not.
MAX_COMMAND_WORDS = 6

#: Openings that make a sentence a question about a preset rather than a request for it. A
#: transcript rarely carries a question mark, so the first word has to carry that job. Sentences
#: caught here are not refused - they go to the model, which can still activate a preset through
#: the `presets.activate` tool. Slower, and right more often.
_QUESTION_OPENERS = frozenset(
    {
        "was",
        "wie",
        "warum",
        "wieso",
        "weshalb",
        "wer",
        "wen",
        "wem",
        "wo",
        "wann",
        "welche",
        "welcher",
        "welches",
        "welchen",
        "kannst",
        "kann",
        "koennen",
        "koenntest",
        "darf",
        "duerfte",
        "soll",
        "sollte",
        "ist",
        "sind",
        "heisst",
        "bedeutet",
        "erklaer",
        "erklaere",
        "erzaehl",
        "erzaehle",
        "weisst",
        "gibt",
        "what",
        "how",
        "why",
        "who",
        "where",
        "when",
        "which",
        "can",
        "could",
        "should",
        "does",
        "do",
        "is",
        "are",
        "explain",
        "tell",
    }
)

#: Which tool each home step calls. The step field names match the tool input models exactly, so
#: the arguments are the step itself minus its discriminator.
_HOME_TOOLS: dict[type, str] = {
    LightStep: "home.light",
    SwitchStep: "home.switch",
    SceneStep: "home.scene",
    ClimateStep: "home.climate",
}


#: The tool a `run` step goes through, so that starting a program is audited and permission
#: checked like everything else. Registered in `nox.presets.tools`.
RUN_ACTION_TOOL = "presets.run_action"


class ToolOutcome(Protocol):
    """The part of a tool result this module reads."""

    ok: bool
    error: str
    data: dict[str, Any] | None


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[ToolOutcome]]
ModeSetter = Callable[[str], Awaitable[None]]
Announcer = Callable[[str], Awaitable[None]]


def normalise(text: str) -> str:
    """Lower-case, strip punctuation and accents, collapse whitespace.

    Accents are folded because speech recognition is not consistent about them, and a preset
    should not depend on whether the transcript wrote "cafe" or "cafe" with an acute.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SPACES.sub(" ", _NOT_WORD.sub(" ", without_accents)).strip()


def match_phrase(text: str, presets: list[PresetConfig]) -> PresetConfig | None:
    """Find the enabled preset whose longest registered phrase occurs in `text`.

    Like the fast path in `nox.ai.fastpath`, this must never swallow a real question. Talking
    *about* a preset is not activating it, so two guards come before the search: a question mark
    rules the sentence out entirely, and anything longer than :data:`MAX_COMMAND_WORDS` words is
    treated as speech rather than as a command. "Gaming mode, bitte" activates; "kannst du mir
    erklaeren, was gaming mode eigentlich macht?" goes to the model, where it belongs.
    """
    if "?" in text:
        return None

    words = normalise(text).split()
    if not words or len(words) > MAX_COMMAND_WORDS or words[0] in _QUESTION_OPENERS:
        return None

    haystack = f" {' '.join(words)} "
    best: PresetConfig | None = None
    best_length = 0
    for preset in presets:
        if not preset.enabled:
            continue
        for phrase in preset.triggers.phrases:
            needle = f" {normalise(phrase)} "
            if len(phrase) > best_length and needle in haystack:
                best, best_length = preset, len(phrase)
    return best


@dataclass(frozen=True, slots=True)
class StepResult:
    """One step of a run - what it was, whether it worked, and how long it took."""

    index: int
    kind: str
    ok: bool
    error: str = ""
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass(frozen=True, slots=True)
class PresetRun:
    """The outcome of activating one preset."""

    preset_id: str
    name: str
    #: How it started: `phrase`, `schedule`, `process` or `manual`.
    trigger: str
    steps: list[StepResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def failures(self) -> list[StepResult]:
        return [step for step in self.steps if not step.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "name": self.name,
            "trigger": self.trigger,
            "ok": self.ok,
            "steps": [step.as_dict() for step in self.steps],
            "duration_ms": round(self.duration_ms, 1),
        }


class PresetRunner:
    """Activates presets. One instance per core, shared by IPC, the tool and the triggers."""

    def __init__(
        self,
        *,
        settings: Callable[[], PresetsConfig],
        call_tool: ToolCaller,
        set_mode: ModeSetter,
        say: Announcer,
    ) -> None:
        self._settings = settings
        self._call_tool = call_tool
        self._set_mode = set_mode
        self._say = say
        #: One lock per preset: saying "gaming mode" twice in a row must not interleave two runs
        #: of the same steps, while two different presets may well overlap.
        self._locks: dict[str, asyncio.Lock] = {}

    def find(self, preset_id: str) -> PresetConfig | None:
        return next((preset for preset in self._settings().items if preset.id == preset_id), None)

    def match(self, text: str) -> PresetConfig | None:
        config = self._settings()
        return match_phrase(text, config.items) if config.enabled else None

    async def activate(
        self, preset_id: str, *, trigger: str = "manual", say: Announcer | None = None
    ) -> PresetRun:
        """Run every step of one preset, recording each, and return what happened.

        `say` redirects everything the preset would speak. A preset started by voice is already a
        conversation turn, so the sentences are collected and answered through that turn instead
        of being spoken a second time over the top of it.
        """
        preset = self.find(preset_id)
        if preset is None:
            raise KeyError(preset_id)

        announce = say or self._say
        lock = self._locks.setdefault(preset.id, asyncio.Lock())
        async with lock:
            started = time.perf_counter()
            results = [
                await self._run_step(index, step, announce)
                for index, step in enumerate(preset.steps, start=1)
            ]
            run = PresetRun(
                preset_id=preset.id,
                name=preset.name,
                trigger=trigger,
                steps=results,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        log.info(
            "presets.activated",
            preset=preset.id,
            trigger=trigger,
            ok=run.ok,
            failed=len(run.failures),
            duration_ms=round(run.duration_ms, 1),
        )
        return run

    async def _run_step(self, index: int, step: PresetStep, say: Announcer) -> StepResult:
        started = time.perf_counter()

        def done(ok: bool, error: str = "") -> StepResult:
            return StepResult(
                index=index,
                kind=step.kind,
                ok=ok,
                error=error,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        try:
            error = await self._perform(step, say)
        except Exception as exc:  # a step must never take the whole run down with it
            log.warning("presets.step_crashed", kind=step.kind, error=str(exc))
            return done(False, f"{step.kind} step failed: {exc}")
        return done(not error, error)

    async def _perform(self, step: PresetStep, say: Announcer) -> str:
        """Carry out one step. Returns an empty string on success, else the reason."""
        if isinstance(step, WaitStep):
            await asyncio.sleep(step.seconds)
            return ""

        if isinstance(step, SayStep):
            await say(step.text)
            return ""

        if isinstance(step, ModeStep):
            await self._set_mode(step.mode)
            return ""

        if isinstance(step, RunStep):
            return await self._perform_run(step, say)

        tool = _HOME_TOOLS[type(step)]
        arguments = step.model_dump(exclude={"kind"}, exclude_none=True)
        outcome = await self._call_tool(tool, arguments)
        return "" if outcome.ok else (outcome.error or f"{tool} failed")

    async def _perform_run(self, step: RunStep, say: Announcer) -> str:
        """Start a registered program - through the tool executor, never around it.

        Routing this through `presets.run_action` rather than calling the starter directly is
        what gives a program start the same permission check, the same audit entry and the same
        kill switch as any other tool call. A preset is a convenience, not an exemption.
        """
        action = next(
            (action for action in self._settings().actions if action.id == step.action), None
        )
        if action is None:
            # Configuration validation rules this out; a live reload could still race with it.
            return f"no action named {step.action!r} is registered"

        outcome = await self._call_tool(RUN_ACTION_TOOL, {"action": step.action})
        result = outcome.data or {}
        if outcome.ok and result.get("ok"):
            return ""

        # Refused, unreachable or failed - from where the user sits these are one thing: the
        # program did not run. Saying so is the difference between a preset that half worked and
        # one that sounds like it fully worked.
        error = str(
            (outcome.error if not outcome.ok else result.get("error"))
            or f"{action.name!r} did not run"
        )
        if action.report_failure:
            await say(error)
        return error
