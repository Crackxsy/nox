"""The core's side of a voice satellite: a node's words become a turn that is answered
sentence by sentence, an echo of the node's own voice is dropped, a flooding node is cut
off, one turn runs at a time, and satellite turns are untrusted."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.events.names import E
from nox.lan.tls import TlsMaterial
from nox.node import protocol as p
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.repo import NodeRepository
from tests.unit.fakes import wait_until
from tests.unit.node.support import session_of, start_node
from tests.unit.node.support_voice import PC, PI, fake_session, heard, voice_hub

pytestmark = pytest.mark.usefixtures("a_screen")


async def test_the_core_answers_a_node_sentence_by_sentence(tmp_path: Path) -> None:
    hub, seen = voice_hub()
    session = fake_session()
    assert await hub.handle(session, p.M_VOICE_HEARD, heard()) == {"accepted": True}
    await wait_until(
        lambda: any(m[1].get("last") for m in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    said = [params for method, params in session.client.sent if method == p.M_VOICE_SAY]
    assert [s["text"] for s in said if s["text"]] == ["Es ist zwölf Uhr.", "Noch etwas?"]
    assert [s["seq"] for s in said] == list(range(len(said))) and said[-1]["last"] is True
    assert not any(s["last"] for s in said[:-1])
    text, _language, channel, origin = seen["turns"][0]
    assert channel == f"voice:node:{PI}" and origin == "node"  # facts wait for confirmation
    # Audited without the words.
    assert seen["audit"][0]["result"] == "16 chars" and "spät" not in json.dumps(seen["audit"])
    await hub.close()


async def test_the_core_takes_no_words_it_may_not_hear() -> None:
    session = fake_session()
    hub, _ = voice_hub(offers=set())
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] == "voice_not_enabled"
    hub, _ = voice_hub(capture="microphone off")
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] == "microphone_off"
    hub, _ = voice_hub()
    answers = [await hub.handle(session, p.M_VOICE_HEARD, heard()) for _ in range(14)]
    assert answers[-1] == {"accepted": False, "reason": "too_many"}
    await hub.close()


async def test_a_failed_turn_is_said_and_a_new_question_cancels_the_old() -> None:
    gate = asyncio.Event()

    async def slow(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        if text == "boom":
            raise RuntimeError("model down")
        await gate.wait()

    hub, _ = voice_hub(turn=slow)
    session = fake_session()
    await hub.handle(session, p.M_VOICE_HEARD, heard("erste"))
    await asyncio.sleep(0.01)
    await hub.handle(session, p.M_VOICE_HEARD, heard("boom"))
    await wait_until(
        lambda: any(m == p.M_VOICE_CANCEL for m, _ in session.client.sent),
        what="the node was never told to cancel the old answer",
    )
    await wait_until(
        lambda: any(params.get("last") for _m, params in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    last = [params for m, params in session.client.sent if m == p.M_VOICE_SAY][-1]
    assert last["text"] == "Das hat nicht funktioniert." and last["last"] is True
    await hub.close()


async def test_a_nodes_speech_level_is_published_for_its_own_pet() -> None:
    hub, seen = voice_hub()
    await hub.handle(fake_session(), p.M_VOICE_LEVEL, p.VoiceLevel(level=0.3))
    name, payload = seen["published"][0]
    assert name == "tts.level" and payload["device"] == PI and payload["level"] == 0.3
    assert payload["channel"] == "node"  # never "private": only that node's own overlay sees it
    hub, seen = voice_hub(offers=set())
    await hub.handle(fake_session(), p.M_VOICE_LEVEL, p.VoiceLevel(level=0.3))
    assert not seen["published"]


async def test_the_microphone_gate_follows_the_cores_privacy() -> None:
    session = fake_session()
    state = {"why": None}
    hub, _ = voice_hub(sessions=[session])
    hub._capture_refusal = lambda: state["why"]  # type: ignore[method-assign]
    await hub.push_gates()
    await hub.push_gates()  # unchanged: said once
    state["why"] = "microphone off (panic)"
    await hub.push_gates()
    gates = [params for method, params in session.client.sent if method == p.M_VOICE_GATE]
    assert [g["capture"] for g in gates] == [True, False]


async def test_the_panic_button_reaches_obs_on_every_node_with_obs() -> None:
    pi, pc = fake_session(PI), fake_session(PC)
    hub, _ = voice_hub(sessions=[pi, pc])
    assert await hub.panic() == 1
    assert pi.client.sent == [(p.M_OBS_PANIC, {"reason": "panic"})] and not pc.client.sent


async def test_the_core_drops_a_nodes_own_voice_as_echo() -> None:
    hub, _ = voice_hub()
    session = fake_session()
    await hub.handle(session, p.M_VOICE_LEVEL, p.VoiceLevel(level=0.4))  # it is speaking
    answer = await hub.handle(session, p.M_VOICE_HEARD, heard())
    assert answer == {"accepted": False, "reason": "echo"}
    later = time.monotonic() + 30.0
    hub._clock = lambda: later
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["accepted"] is True
    await wait_until(
        lambda: any(params.get("last") for _m, params in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    # The answer it was sent counts as its speech too.
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] == "echo"
    await hub.close()


async def test_voice_from_the_pc_nox_controls_is_refused() -> None:
    from nox.node.world_routes import NodeControl

    session = fake_session()
    control = NodeControl(None, lambda _payload: None)
    hub, _ = voice_hub(sessions=[session])
    hub.controlled = control.locked
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["accepted"] is True
    control.answered(PI, {"gen": 5, "active": True, "id": "ab" * 16})  # a session runs there
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] == "controlled_here"
    await hub.push_gates()
    gate = [params for m, params in session.client.sent if m == p.M_VOICE_GATE][-1]
    assert gate == {"capture": False, "reason": "controlled_here"}

    def broken(_device: str) -> bool:
        raise RuntimeError("cannot tell")

    hub.controlled = broken
    assert (await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] == "controlled_here"
    await hub.close()


async def test_the_rate_limit_comes_first_and_refusals_are_audited_sparingly() -> None:
    hub, seen = voice_hub(offers=set())
    session = fake_session()
    reasons = [(await hub.handle(session, p.M_VOICE_HEARD, heard()))["reason"] for _ in range(20)]
    assert reasons[:12] == ["voice_not_enabled"] * 12 and set(reasons[12:]) == {"too_many"}
    denied = [a for a in seen["audit"] if a["decision"] == "deny"]
    assert len(denied) == 2  # once per reason in the interval
    later = time.monotonic() + 100.0
    hub._clock = lambda: later
    await hub.handle(session, p.M_VOICE_HEARD, heard())
    later = [a["result"] for a in seen["audit"] if a["decision"] == "deny"][-1]
    assert "more)" in later  # the count of what was not written


async def test_a_flooding_node_is_cut_off(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    async def on_satellite(session: Any, method: str, params: Any) -> dict[str, Any] | None:
        return {"accepted": False, "reason": "too_many"}

    listener.on_satellite = on_satellite
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        await wait_until(
            lambda: bool(node.agent.features),
            what="the node never learned which features the core has",
        )
        heard = {"turn_id": "e" * 32, "text": "Hallo", "language": "de"}
        for _ in range(31):
            await node.agent.ask_core(p.M_VOICE_HEARD, heard, timeout_s=2)
        assert session.violations >= 1
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 10)


async def test_a_node_turn_waits_and_cancels_nothing_elsewhere() -> None:
    from tests.unit.fakes import make_orchestrator as make

    orch, _bus, router, speaker, _turns = make(speak=False)
    await orch.start()
    router.delay = 0.02
    chat = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:x"))
    await asyncio.sleep(0.05)
    node = asyncio.create_task(
        orch.handle_text(
            "Erklär mir kurz Rust",
            channel=f"voice:node:{PI}",
            barge_in=False,
            context_note="from a voice satellite",
        )
    )
    chat_turn = await chat
    assert chat_turn.response.startswith("Hallo.")  # not cancelled by the node's question
    node_turn = await node
    assert node_turn.response and node_turn.conversation.startswith(f"voice:node:{PI}#")
    assert speaker.interrupts.count("new_input") == 1  # the chat's own start; none for the node
    assert "from a voice satellite" in router.requests[-1].context
    await orch.stop()


# ---- during a computer-use session: refusal, saturation, taint -----------------------------------


def _session_control() -> Any:
    from nox.node.world_routes import NodeControl

    control = NodeControl(None, lambda _payload: None)
    control.answered(PC, {"gen": 2, "active": True, "id": "ab" * 16})  # Nox controls the PC
    return control


async def test_a_session_on_one_pc_keeps_that_pc_quiet_and_saturation_all_of_them() -> None:
    from nox.node.install import attach_control
    from nox.node.world_routes import REFUSE_SATURATED, session_refusal

    control = _session_control()
    hub, _ = voice_hub(offers={(PI, "voice.say"), (PC, "voice.say")})
    attach_control(hub, control)
    pc, pi = fake_session(PC), fake_session(PI)
    assert (await hub.handle(pc, p.M_VOICE_HEARD, heard()))["reason"] == "controlled_here"
    assert (await hub.handle(pi, p.M_VOICE_HEARD, heard()))["accepted"] is True
    assert hub.session_active()  # the other node's turn is marked untrusted (see `turn`)
    # A binding that is full: nobody can be told apart from the controlled PC - neither the
    # dashboard (#167) nor any satellite.
    control.saturated.add(PC)
    ctx = SimpleNamespace(role="dashboard", client_id="dashboard:1", peer="")
    refusal = await session_refusal(
        control, ctx, None, action="chat.send", gate=None, device_guard=None, remote=lambda: False
    )
    assert refusal == REFUSE_SATURATED
    assert (await hub.handle(pi, p.M_VOICE_HEARD, heard()))["reason"] == "controlled_here"
    await hub.close()


async def test_voice_during_a_session_is_marked_like_paired_device_chat(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from nox.node.install import NodeRuntime, attach_control, install_satellites
    from nox.node.registry import NodeRegistry
    from tests.unit.node.support import pair_node

    marked: list[tuple[str, Any]] = []
    registry = NodeRegistry(pairing, nodes)
    pi = (await pair_node(pairing, tmp_path, name="Living room Pi")).node_id

    async def handle_text(text: str, **kw: Any) -> Any:
        return SimpleNamespace(response="")

    engine = SimpleNamespace(
        mark_tainted=lambda conversation, content: marked.append((conversation, content)),
        satellite_label=None,
    )
    orchestrator = SimpleNamespace(
        handle_text=handle_text, conversation_for=lambda channel: f"{channel}#c0ffee"
    )
    core = SimpleNamespace(
        security=SimpleNamespace(privacy=None, killswitch=None, audit=None, engine=engine),
        orchestrator=orchestrator,
        tokens=None,
        hub=None,
        bus=None,
    )
    listener = SimpleNamespace(sessions={}, _on_connected=None, _on_disconnected=None)
    runtime = NodeRuntime(pairing=pairing, enabled=True)
    satellites = install_satellites(core, runtime, registry, listener, pairing)
    attach_control(satellites, _session_control())
    await satellites._turn("Hallo", "de", lambda _d: None, f"voice:node:{pi}", "note")  # type: ignore[misc,arg-type]
    assert marked == [(f"voice:node:{pi}#c0ffee", None)]
    await runtime.stop()


# ---- presets, one turn at a time, and what a node is told to say ---------------------------------


class _Gates:
    def __init__(self) -> None:
        self.preset: list[str] = []
        self.satellite: list[tuple[str, str]] = []
        self.mode: list[str] = []

    async def handle(self, text: str, language: str) -> str | None:
        self.preset.append(text)
        return "Gaming ist an."

    async def handle_from_satellite(
        self, text: str, language: str, *, conversation: str
    ) -> str | None:
        self.satellite.append((text, conversation))
        return None


class _ModeGate:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def handle(self, text: str, language: str) -> str | None:
        self.log.append(text)
        return "Stream-Modus an."


async def test_satellite_words_skip_presets_modes_and_new_conversation() -> None:
    from tests.unit.fakes import make_orchestrator as make

    orch, _bus, router, _speaker, _turns = make(speak=False)
    gates = _Gates()
    orch.preset_gate = gates
    orch.mode_gate = _ModeGate(gates.mode)
    await orch.start()
    node = f"voice:node:{PI}"
    turn = await orch.handle_text("gaming mode", channel=node, barge_in=False)
    assert gates.preset == [] and gates.mode == []  # neither shortcut runs on room words
    assert gates.satellite == [("gaming mode", turn.conversation)]
    assert turn.fast_path == "" and router.requests  # the model, as an untrusted turn
    before = turn.conversation
    again = await orch.handle_text("neues Gespräch", channel=node, barge_in=False)
    assert again.conversation == before  # not discarded by a voice in the room
    chat = await orch.handle_text("gaming mode", channel="chat:x")
    assert gates.preset == ["gaming mode"] and chat.fast_path == "preset"
    await orch.stop()


async def test_only_a_preset_marked_for_satellites_runs_there_as_an_untrusted_turn() -> None:
    from nox.core.config.presets import PresetsConfig
    from nox.presets.engine import PresetRunner
    from nox.presets.gate import FROM_SATELLITE, VoicePresetGate
    from nox.security.taint import current_conversation

    calls: list[tuple[str, bool, str]] = []
    modes: list[str] = []

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        calls.append((name, FROM_SATELLITE.get(), current_conversation()))
        return SimpleNamespace(ok=True, data={}, error=None)

    async def set_mode(mode: str) -> None:
        if FROM_SATELLITE.get():
            raise ValueError("a voice satellite never switches the mode")
        modes.append(mode)

    async def say(_text: str) -> None:
        return None

    def preset(pid: str, phrase: str, from_satellites: bool) -> dict[str, Any]:
        return {
            "id": pid,
            "name": pid.title(),
            "from_satellites": from_satellites,
            "triggers": {"phrases": [phrase]},
            "steps": [
                {"kind": "scene", "entity_id": "scene.living_room"},
                {"kind": "mode", "mode": "stream"},
            ],
        }

    config = PresetsConfig.model_validate(
        {"items": [preset("cosy", "kuschelabend", True), preset("tv", "fernsehabend", False)]}
    )
    runner = PresetRunner(settings=lambda: config, call_tool=call_tool, set_mode=set_mode, say=say)
    gate = VoicePresetGate(runner)
    node = f"voice:node:{PI}#c0ffee"
    assert await gate.handle_from_satellite("fernsehabend", "de", conversation=node) is None
    assert calls == []  # not marked: nothing runs from a satellite
    answer = await gate.handle_from_satellite("kuschelabend", "de", conversation=node)
    assert calls == [("home.scene", True, node)]  # as an untrusted turn's, in its conversation
    assert modes == [] and answer is not None and "nicht geklappt" in answer  # never the mode
    assert current_conversation() == "" and FROM_SATELLITE.get() is False


@pytest.mark.parametrize("value", ["true", 1, "yes", "on"])
def test_only_a_real_boolean_marks_a_preset_for_satellites(value: Any) -> None:
    from pydantic import ValidationError

    from nox.core.config.presets import PresetConfig

    with pytest.raises(ValidationError):
        PresetConfig.model_validate(
            {
                "id": "cosy",
                "name": "Cosy",
                "from_satellites": value,
                "triggers": {"phrases": ["kuschelabend"]},
                "steps": [{"kind": "scene", "entity_id": "scene.living_room"}],
            }
        )


async def test_one_turn_runs_at_a_time_and_a_kill_cancels_it() -> None:
    from nox.core.events import Event as CoreEvent
    from tests.unit.fakes import make_orchestrator as make

    orch, bus, router, speaker, _turns = make(speak=False)
    running = {"now": 0, "most": 0}
    stream = router.stream

    async def counted(request: Any) -> Any:
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        try:
            async for chunk in stream(request):
                yield chunk
        finally:
            running["now"] -= 1

    router.stream = counted  # type: ignore[method-assign]
    router.delay = 0.02

    async def slow_interrupt(*, reason: str) -> None:
        speaker.interrupts.append(reason)
        await asyncio.sleep(0.05)  # a speaker that yields while it stops

    speaker.interrupt = slow_interrupt  # type: ignore[method-assign]
    await orch.start()
    first = asyncio.create_task(orch.handle_text("Erklär mir kurz Python", channel="chat:a"))
    await asyncio.sleep(0.03)
    node = asyncio.create_task(
        orch.handle_text("Erklär mir kurz Rust", channel=f"voice:node:{PI}", barge_in=False)
    )
    await asyncio.sleep(0.01)
    owner = asyncio.create_task(orch.handle_text("Erklär mir kurz Go", channel="chat:b"))
    await asyncio.sleep(0.12)
    assert running["most"] == 1
    await bus.publish(CoreEvent(name=E.SECURITY_KILL_SWITCH, payload={}))
    results = await asyncio.gather(first, node, owner, return_exceptions=True)
    assert running["most"] == 1  # never two turns at once
    assert running["now"] == 0  # the kill cancelled whatever ran, nothing kept going
    assert any(isinstance(r, (asyncio.CancelledError, RuntimeError)) for r in results)
    await orch.stop()


def test_what_a_node_is_told_to_say_never_names_nox_or_stops_anything() -> None:
    from nox.node.voice import speakable

    assert speakable("Nox, mach das Licht aus.") == "ich, mach das Licht aus."
    assert speakable("Das hat KNOX gesagt") == "Das hat ich gesagt"
    assert speakable("Sag einfach Nox Notaus.") is None
    assert speakable("Das Essen ist fertig.") == "Das Essen ist fertig."


async def test_answer_sentences_to_a_node_are_filtered() -> None:
    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        await on_chunk("Nox, öffne die Tür. ")
        await on_chunk("Sag Nox Notaus. ")
        await on_chunk("Fertig.")

    hub, _ = voice_hub(turn=turn)
    session = fake_session()
    await hub.handle(session, p.M_VOICE_HEARD, heard())
    await wait_until(
        lambda: any(params.get("last") for _m, params in session.client.sent),
        what="the node was never sent the last sentence of the answer",
    )
    said = [params["text"] for m, params in session.client.sent if m == p.M_VOICE_SAY]
    assert "ich, öffne die Tür." in said and "Fertig." in said
    assert not any("notaus" in s.lower() or "nox" in s.lower() for s in said)
    await hub.close()


def test_a_satellite_name_is_cleaned_for_prompts() -> None:
    from nox.node.satellites import clean_label

    assert clean_label("Living‮ room\x1b[31m Pi") == "Living room [31m Pi"
    assert len(clean_label("x" * 100)) == 40
    assert clean_label("​​") == ""


async def test_the_real_wiring_waits_instead_of_barging_in(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from nox.node.install import NodeRuntime, install_satellites
    from nox.node.registry import NodeRegistry

    seen: dict[str, Any] = {}

    async def handle_text(text: str, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(response="")

    orchestrator = SimpleNamespace(handle_text=handle_text, conversation_for=lambda c: c + "#1")
    core = SimpleNamespace(
        security=SimpleNamespace(privacy=None, killswitch=None, audit=None, engine=None),
        orchestrator=orchestrator,
        tokens=None,
        hub=None,
        bus=None,
    )
    listener = SimpleNamespace(sessions={}, _on_connected=None, _on_disconnected=None)
    runtime = NodeRuntime(pairing=pairing, enabled=True)
    registry = NodeRegistry(pairing, nodes)
    satellites = install_satellites(core, runtime, registry, listener, pairing)
    assert satellites._turn is not None
    await satellites._turn("Hallo", "de", lambda _d: None, f"voice:node:{PI}", "note")  # type: ignore[arg-type]
    assert seen["barge_in"] is False and seen["context_note"] == "note"
    assert seen["speak"] is False and seen["channel"] == f"voice:node:{PI}"
    await runtime.stop()
