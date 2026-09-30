"""Presets: one spoken phrase that reaches several systems at once.

A preset is a name, the ways it can be triggered, and an ordered list of steps. A step either
speaks to the user's Home Assistant, switches the assistant's own mode, or runs one of the
`actions` the user registered - never an arbitrary command.

That last point is the whole security design of this section. The language model can ask for
`presets.activate {id}` and nothing else: it cannot pass a command line, a path or an argument.
Which program a `run` step starts is decided here, in configuration the user edits, so a prompt
that talks Nox into activating a preset can only reach something the user already built on
purpose. The boundary sits under the model rather than in its instructions - the same reason
`nox.home.boundary` exists.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from nox.core.config.types import StrictSection

__all__ = [
    "ClimateStep",
    "LightStep",
    "ModeStep",
    "PresetActionConfig",
    "PresetConfig",
    "PresetStep",
    "PresetTriggerConfig",
    "PresetsConfig",
    "RunStep",
    "SayStep",
    "SceneStep",
    "SwitchStep",
    "WaitStep",
]

#: Identifiers are typed by hand into YAML and referred to from steps, so they stay short and
#: unambiguous rather than being free text.
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,39}$"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Below this, a phrase would fire on ordinary speech rather than on a deliberate command.
MIN_PHRASE_LENGTH = 3


# ---- steps ---------------------------------------------------------------------------------


class LightStep(StrictSection):
    """Set lights: on/off, brightness, colour temperature."""

    kind: Literal["light"] = "light"
    entity_ids: list[str] = Field(min_length=1, max_length=50)
    on: bool | None = None
    brightness_pct: int | None = Field(default=None, ge=0, le=100)
    color_temp_kelvin: int | None = Field(default=None, ge=1500, le=6600)

    @model_validator(mode="after")
    def _does_something(self) -> LightStep:
        if self.on is None and self.brightness_pct is None and self.color_temp_kelvin is None:
            raise ValueError("a light step must set at least one of on, brightness or colour")
        return self


class SwitchStep(StrictSection):
    """Switch sockets and other plain on/off entities."""

    kind: Literal["switch"] = "switch"
    entity_ids: list[str] = Field(min_length=1, max_length=50)
    on: bool


class SceneStep(StrictSection):
    """Activate a scene the user defined inside Home Assistant."""

    kind: Literal["scene"] = "scene"
    entity_id: str = Field(min_length=3, max_length=255)


class ClimateStep(StrictSection):
    """Set a target temperature. Field names and bounds follow `home.climate` exactly."""

    kind: Literal["climate"] = "climate"
    entity_ids: list[str] = Field(min_length=1, max_length=50)
    temperature_c: float = Field(ge=4.0, le=35.0)


class ModeStep(StrictSection):
    """Switch Nox's own mode, which decides how loudly and how often it speaks."""

    kind: Literal["mode"] = "mode"
    mode: str = Field(min_length=2, max_length=40)


class RunStep(StrictSection):
    """Run one of the registered actions - by identifier, never by command line."""

    kind: Literal["run"] = "run"
    action: str = Field(pattern=ID_PATTERN)


class SayStep(StrictSection):
    """Have Nox say one sentence, so an invisible change is still noticeable."""

    kind: Literal["say"] = "say"
    text: str = Field(min_length=1, max_length=280)


class WaitStep(StrictSection):
    """Pause between steps, for devices that need a moment before the next command."""

    kind: Literal["wait"] = "wait"
    seconds: float = Field(gt=0.0, le=30.0)


PresetStep = Annotated[
    LightStep | SwitchStep | SceneStep | ClimateStep | ModeStep | RunStep | SayStep | WaitStep,
    Field(discriminator="kind"),
]


# ---- actions, triggers, presets -------------------------------------------------------------


class PresetActionConfig(StrictSection):
    """A program the user allows Nox to start, under a name a preset can refer to.

    `command` is a list - program first, then arguments - and is handed to the operating system
    exactly like that, with no shell in between. Without a shell there is no quoting to get wrong
    and no `&&` to append a second command, so an argument stays an argument even when it contains
    spaces or ampersands.

    The program needs an absolute path, because a bare name would be resolved through `PATH` and
    would then mean different programs depending on how Nox happened to be started.
    """

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    command: list[str] = Field(min_length=1, max_length=32)
    working_dir: str = ""
    timeout_s: float = Field(default=20.0, gt=0.0, le=300.0)
    #: Say something when the program fails. Off for actions that are expected to be noisy.
    report_failure: bool = True

    @field_validator("command")
    @classmethod
    def _program_is_absolute(cls, value: list[str]) -> list[str]:
        program = value[0].strip()
        if not program:
            raise ValueError("the first entry of command must be the program to run")
        if not PureWindowsPath(program).is_absolute():
            raise ValueError(
                f"command must start with an absolute path to the program, got {program!r} - "
                "a bare name would be resolved through PATH and could mean different programs "
                "depending on how Nox was started"
            )
        return [program, *(argument.strip() for argument in value[1:])]


class PresetTriggerConfig(StrictSection):
    """The ways one preset can start. Every one of them is optional.

    Phrases are matched before the language model is asked anything, so saying "gaming mode" costs
    a string comparison rather than a round trip.
    """

    phrases: list[str] = Field(default_factory=list, max_length=20)
    #: Local time of day, `HH:MM`. Empty means the preset has no schedule.
    at: str = Field(default="", pattern=r"^$|^([01]\d|2[0-3]):[0-5]\d$")
    #: Days the schedule applies to. Empty means every day.
    days: list[str] = Field(default_factory=list, max_length=7)
    #: Process name, e.g. `RocketLeague.exe`. Needs `sensors.game.process_names` to list it too.
    on_process_start: str = Field(default="", max_length=120)
    on_process_end: str = Field(default="", max_length=120)

    @field_validator("phrases")
    @classmethod
    def _phrases_are_usable(cls, value: list[str]) -> list[str]:
        cleaned = [phrase.strip().lower() for phrase in value if phrase.strip()]
        too_short = [phrase for phrase in cleaned if len(phrase) < MIN_PHRASE_LENGTH]
        if too_short:
            raise ValueError(
                f"trigger phrase(s) {too_short} are shorter than {MIN_PHRASE_LENGTH} characters "
                "and would fire on ordinary speech"
            )
        return cleaned

    @field_validator("days")
    @classmethod
    def _days_are_known(cls, value: list[str]) -> list[str]:
        cleaned = [day.strip().lower()[:3] for day in value if day.strip()]
        unknown = [day for day in cleaned if day not in WEEKDAYS]
        if unknown:
            raise ValueError(f"unknown weekday(s) {unknown}, expected any of {list(WEEKDAYS)}")
        return cleaned

    @model_validator(mode="after")
    def _days_need_a_time(self) -> PresetTriggerConfig:
        if self.days and not self.at:
            raise ValueError("days were given without a time - set `at` as well")
        return self


class PresetConfig(StrictSection):
    """One preset: what it is called, how it starts, and what it does."""

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    triggers: PresetTriggerConfig = Field(default_factory=PresetTriggerConfig)
    steps: list[PresetStep] = Field(min_length=1, max_length=40)


class PresetsConfig(StrictSection):
    """Presets and the programs they are allowed to start."""

    enabled: bool = True
    #: Programs Nox may start, each under a name. Empty by default: a fresh installation can
    #: switch lights and modes, and cannot run anything at all until the user adds an entry.
    actions: list[PresetActionConfig] = Field(default_factory=list, max_length=50)
    items: list[PresetConfig] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _references_resolve(self) -> PresetsConfig:
        _reject_duplicates([action.id for action in self.actions], "actions")
        _reject_duplicates([preset.id for preset in self.items], "presets")
        _reject_duplicates(
            [phrase for preset in self.items for phrase in preset.triggers.phrases],
            "trigger phrases",
        )

        known = {action.id for action in self.actions}
        for preset in self.items:
            for position, step in enumerate(preset.steps, start=1):
                if isinstance(step, RunStep) and step.action not in known:
                    raise ValueError(
                        f"preset {preset.id!r} step {position} runs unknown action "
                        f"{step.action!r}; known actions: {sorted(known) or 'none'}"
                    )
        return self


def _reject_duplicates(values: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate entry in {what}: {value!r}")
        seen.add(value)
