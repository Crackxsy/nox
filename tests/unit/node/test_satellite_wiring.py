"""Satellites through the real wiring of `nox.node.hub.install`: a preset started at a satellite
runs as the satellite agent, dropped sentences are skipped instead of sent empty, and the
hub cleans a node's name itself."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.events.names import E
from nox.node.wire import protocol as p
from tests.unit.fakes import FakeBus, wait_until
from tests.unit.node.support_voice import PI, fake_session, heard, running_satellite, voice_hub

# ---- presets from a voice satellite, through the real wiring ---------------------


def _preset_core(executor: Any) -> Any:
    from nox.core.config.presets import PresetsConfig
    from nox.ipc.dispatch import RequestRegistry
    from nox.tools.registry import ToolRegistry

    presets = PresetsConfig.model_validate(
        {
            "items": [
                {
                    "id": "cosy",
                    "name": "Cosy",
                    "from_satellites": True,
                    "triggers": {"phrases": ["kuschelabend"]},
                    "steps": [
                        {"kind": "scene", "entity_id": "scene.living_room"},
                        {"kind": "mode", "mode": "stream"},
                    ],
                }
            ]
        }
    )
    return SimpleNamespace(
        config=SimpleNamespace(presets=presets, identity=SimpleNamespace(ui_language="de")),
        bus=FakeBus(),
        state=SimpleNamespace(get=lambda _key: "companion"),
        security=SimpleNamespace(engine=None),
        speaker=None,
        orchestrator=SimpleNamespace(preset_gate=None),
        tool_registry=ToolRegistry(),
        tool_executor=executor,
        registry=RequestRegistry(),
    )


async def test_a_satellite_preset_runs_its_steps_as_the_satellite_agent_through_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nox.presets.install as presets_install
    from nox.presets.gate import SATELLITE_AGENT
    from nox.security.taint import current_conversation

    calls: list[tuple[str, str, str]] = []
    switched: list[str] = []

    class Executor:
        async def call(self, *, agent: str, name: str, arguments: dict[str, Any], mode: str) -> Any:
            calls.append((agent, name, current_conversation()))
            return SimpleNamespace(ok=True, data={}, error=None)

    async def switch_mode(_state: Any, _engine: Any, _bus: Any, mode: Any, *, by: str) -> None:
        switched.append(str(mode.value))

    monkeypatch.setattr(presets_install, "switch_mode", switch_mode)
    core = _preset_core(Executor())
    runtime = presets_install.install(core)
    gate = core.orchestrator.preset_gate
    node = f"voice:node:{PI}#c0ffee"
    try:
        answer = await gate.handle_from_satellite("kuschelabend", "de", conversation=node)
        # The step goes out as `presets.satellite`, in the node's (tainted) conversation.
        assert calls == [(SATELLITE_AGENT, "home.scene", node)]
        # The mode step is refused by the real `set_mode`, and that is reported.
        assert switched == [] and answer is not None and "nicht geklappt" in answer
        # The same preset said by the owner: the ordinary agent, and the mode does switch.
        await gate.handle("kuschelabend", "de")
        assert calls[-1] == ("presets", "home.scene", "")
        assert switched == ["stream"]
    finally:
        await runtime.stop()


# ---- what the core sends a node to say and the name in the model's context --------


async def test_a_dropped_sentence_in_the_middle_is_skipped_not_sent_empty() -> None:
    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        await on_chunk("Gern. ")
        await on_chunk("Sag einfach Nox Notaus. ")  # dropped: a kill phrase
        await on_chunk("Fertig.")

    hub, _ = voice_hub(turn=turn)
    session = fake_session()
    await hub.handle(session, p.M_VOICE_HEARD, heard())
    await wait_until(
        lambda: any(params.get("last") for _m, params in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    said = [params for method, params in session.client.sent if method == p.M_VOICE_SAY]
    spoken = [s["text"] for s in said if s["text"]]
    assert spoken == ["Gern.", "Fertig."]
    assert [s["seq"] for s in said] == list(range(len(said))) and said[-1]["last"] is True
    assert not any(not s["text"] and not s["last"] for s in said)  # never an empty sentence
    await hub.close()


async def test_a_dropped_last_sentence_still_ends_the_answer() -> None:
    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        await on_chunk("Gern. ")
        await on_chunk("Sag einfach Nox Notaus.")

    hub, _ = voice_hub(turn=turn)
    session = fake_session()
    await hub.handle(session, p.M_VOICE_HEARD, heard())
    await wait_until(
        lambda: any(params.get("last") for _m, params in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    said = [params for method, params in session.client.sent if method == p.M_VOICE_SAY]
    assert [(s["text"], s["last"]) for s in said] == [("Gern.", False), ("", True)]
    await hub.close()


async def test_the_hub_cleans_the_nodes_name_itself() -> None:
    from nox.node.hub.satellites import SatelliteHub

    notes: list[str] = []

    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        notes.append(note)

    hub, _ = voice_hub(turn=turn)
    assert isinstance(hub, SatelliteHub)
    hub._label = lambda _device: "Living\u202e room\x1b[2J Pi" + "x" * 80  # noqa: SLF001
    session = fake_session()
    await hub.handle(session, p.M_VOICE_HEARD, heard())
    await wait_until(lambda: bool(notes), what="the hub never built the note for the model")
    assert "\u202e" not in notes[0] and "\x1b" not in notes[0]
    assert "Living room [2J Pi" in notes[0]
    assert "x" * 41 not in notes[0]  # cut to the label's limit
    await hub.close()


# ---- the node's own voice coming back -------------------------------------------------


async def test_a_segment_begun_during_speech_is_dropped_even_after_the_echo_tail(
    tmp_path: Path,
) -> None:
    satellite, seen = await running_satellite(tmp_path)
    clock = {"now": 100.0}
    satellite.clock = lambda: clock["now"]
    satellite.pipeline._speaking = True  # noqa: SLF001 - the device is answering
    await satellite._on_event(E.VOICE_INPUT_STARTED, {"forced": False})  # its own voice begins
    satellite.pipeline._speaking = False  # noqa: SLF001
    await satellite._on_event(E.TTS_FINISHED, {"utterance_id": "u", "ok": True})
    clock["now"] += 5.0  # the transcript only arrives long after the tail
    heard = {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert seen["asked"] == []  # it began as echo: never a question
    # The next segment, begun in silence, is a question again.
    await satellite._on_event(E.VOICE_INPUT_STARTED, {"forced": False})
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert len(seen["asked"]) == 1
    await satellite.shutdown()
