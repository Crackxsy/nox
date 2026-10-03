"""Spoken mode switches: what counts as a command, what is left to the model, what Nox says."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nox.core.mode_intents import ModeGate, PluginStatus, match_mode_intent
from nox.core.state import Mode


@pytest.mark.parametrize(
    "text",
    [
        "Lass uns streamen.",
        "lass uns streamen",
        "Nox, lass uns streamen!",
        "Lass uns live gehen",
        "Ich will heute Minecraft streamen.",
        "Wir gehen live",
        "Starte den Stream",
        "Stream-Modus",
        "Streammodus an",
        "Let's stream",
    ],
)
def test_these_start_the_stream(text: str) -> None:
    assert match_mode_intent(text) is Mode.STREAM


@pytest.mark.parametrize(
    "text",
    [
        "Stream beenden",
        "Beende den Stream",
        "Der Stream ist vorbei",
        "Begleitermodus",
        "Stop the stream",
    ],
)
def test_these_end_the_stream(text: str) -> None:
    assert match_mode_intent(text) is Mode.COMPANION


@pytest.mark.parametrize(
    "text",
    [
        "Wann wollen wir streamen?",
        "Sollen wir heute streamen",
        "Ich will heute nicht streamen",
        "Gestern haben wir gestreamt",
        "Wie viele Zuschauer hatte der Stream gestern?",
        "Ich hab mir gestern einen richtig guten Stream von einem Freund angeschaut, echt gut",
        "Hallo Nox",
        "",
    ],
)
def test_questions_and_talk_about_streaming_go_to_the_model(text: str) -> None:
    assert match_mode_intent(text) is None


class _Core:
    def __init__(self, mode: str, plugins: Sequence[PluginStatus]) -> None:
        self.mode = mode
        self.plugins = list(plugins)
        self.switched: list[Mode] = []

    async def switch(self, mode: Mode) -> str:
        self.switched.append(mode)
        self.mode = mode.value
        return "stream" if mode is Mode.STREAM else "companion"

    def gate(self) -> ModeGate:
        async def no_wait(_s: float) -> None:
            return None

        return ModeGate(
            switch=self.switch,
            current_mode=lambda: self.mode,
            stream_plugins=lambda: self.plugins,
            sleep=no_wait,
        )


async def test_starting_says_what_runs_and_what_does_not() -> None:
    core = _Core(
        "companion",
        [PluginStatus("twitch", True), PluginStatus("obs", False, "OBS is not reachable")],
    )
    answer = await core.gate().handle("Lass uns streamen", "de")
    assert core.switched == [Mode.STREAM]
    assert answer == "Stream-Modus ist an. Twitch läuft. OBS läuft nicht: OBS is not reachable."


async def test_without_stream_plugins_it_says_so_instead_of_pretending() -> None:
    core = _Core("companion", [])
    answer = await core.gate().handle("Lass uns streamen", "de")
    assert answer is not None and "kein Stream-Plugin" in answer


async def test_asking_again_while_streaming_does_not_switch_twice() -> None:
    core = _Core("stream", [PluginStatus("twitch", True), PluginStatus("obs", True)])
    answer = await core.gate().handle("Lass uns streamen", "de")
    assert core.switched == []
    assert answer == "Wir sind schon im Stream-Modus. Twitch und OBS laufen."


async def test_ending_switches_back_only_while_streaming() -> None:
    core = _Core("stream", [])
    assert await core.gate().handle("Stream beenden", "de") is not None
    assert core.switched == [Mode.COMPANION]
    idle = _Core("companion", [])
    assert await idle.gate().handle("Begleitermodus", "de") is None
    assert idle.switched == []


async def test_waits_for_plugins_that_are_still_starting() -> None:
    core = _Core("companion", [PluginStatus("twitch", False)])
    calls = 0

    async def sleep(_s: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            core.plugins = [PluginStatus("twitch", True)]

    gate = ModeGate(
        switch=core.switch,
        current_mode=lambda: core.mode,
        stream_plugins=lambda: core.plugins,
        sleep=sleep,
    )
    answer = await gate.handle("Lass uns streamen", "de")
    assert calls == 3 and answer == "Stream-Modus ist an. Twitch läuft."
