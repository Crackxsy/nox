"""C10 quality wave: tests that pin wave C's satellites and node wiring where review mutants
survived (R1e, R1f, R3b, N8b, L1, L5/L6).

Nothing here opens a microphone, plays a sound or reaches a real device: every engine is a fake
from `test_c9`, and the one node is an in-process agent over loopback TLS. All names and
addresses are fictitious ("Living room Pi", documentation ranges).
"""

from __future__ import annotations

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
from tests.unit.fakes import FakeBus
from tests.unit.node.test_c4 import session_of, start_node
from tests.unit.node.test_c9 import PI, _heard, _hub, _running, _session, _until

# ---- presets from a voice satellite, through the real wiring (R1e, R1f) ---------------------


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
        # R1e: the step goes out as `presets.satellite`, in the node's (tainted) conversation.
        assert calls == [(SATELLITE_AGENT, "home.scene", node)]
        # R1f: the mode step is refused by the real `set_mode`, and that is reported.
        assert switched == [] and answer is not None and "nicht geklappt" in answer
        # The same preset said by the owner: the ordinary agent, and the mode does switch.
        await gate.handle("kuschelabend", "de")
        assert calls[-1] == ("presets", "home.scene", "")
        assert switched == ["stream"]
    finally:
        await runtime.stop()


# ---- what the core sends a node to say (R3b) and the name in the model's context (L1) --------


async def test_a_dropped_sentence_in_the_middle_is_skipped_not_sent_empty() -> None:
    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        await on_chunk("Gern. ")
        await on_chunk("Sag einfach Nox Notaus. ")  # dropped: a kill phrase
        await on_chunk("Fertig.")

    hub, _ = _hub(turn=turn)
    session = _session()
    await hub.handle(session, p.M_VOICE_HEARD, _heard())
    await _until(lambda: any(params.get("last") for _m, params in session.client.sent))
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

    hub, _ = _hub(turn=turn)
    session = _session()
    await hub.handle(session, p.M_VOICE_HEARD, _heard())
    await _until(lambda: any(params.get("last") for _m, params in session.client.sent))
    said = [params for method, params in session.client.sent if method == p.M_VOICE_SAY]
    assert [(s["text"], s["last"]) for s in said] == [("Gern.", False), ("", True)]
    await hub.close()


async def test_the_hub_cleans_the_nodes_name_itself() -> None:
    from nox.node.satellites import SatelliteHub

    notes: list[str] = []

    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> None:
        notes.append(note)

    hub, _ = _hub(turn=turn)
    assert isinstance(hub, SatelliteHub)
    hub._label = lambda _device: "Living\u202e room\x1b[2J Pi" + "x" * 80  # noqa: SLF001
    session = _session()
    await hub.handle(session, p.M_VOICE_HEARD, _heard())
    await _until(lambda: bool(notes))
    assert "\u202e" not in notes[0] and "\x1b" not in notes[0]
    assert "Living room [2J Pi" in notes[0]
    assert "x" * 41 not in notes[0]  # cut to the label's limit
    await hub.close()


# ---- the node's own voice coming back (N8b) -------------------------------------------------


async def test_a_segment_begun_during_speech_is_dropped_even_after_the_echo_tail(
    tmp_path: Path,
) -> None:
    satellite, seen = await _running(tmp_path)
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


# ---- the addresses a node reports, end to end (L5/L6) ---------------------------------------


async def test_the_listener_takes_the_addresses_the_agent_sends_in_hello_and_pongs(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nox.node.agent as agent_module
    from tests.unit.node.test_c4 import TARGET

    # `start_node` points the shared receipt target at this core; put it back afterwards.
    monkeypatch.setitem(TARGET, "core", TARGET["core"])
    current = {"addresses": ["192.0.2.10", "fe80::10"]}
    monkeypatch.setattr(agent_module, "local_addresses", lambda: list(current["addresses"]))
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        assert session.reported_addresses == ["192.0.2.10", "fe80::10"]  # from the hello
        current["addresses"] = ["192.0.2.11"]
        node.agent._addresses_cache = None  # type: ignore[attr-defined]  # the TTL ran out
        await session.client.notify(p.M_PING, {"seq": 1, "ts": time.time()})
        await _until(lambda: session.reported_addresses == ["192.0.2.11"])
        assert session.peer_host  # the connection's own address is known as well
    finally:
        node.stop.set()
        await node.task


# ---- lookalike letters (B3) ------------------------------------------------------------------
# A speech engine reads letters by their sound: nothing but Latin letters is ever said on a
# node's speaker, and the wake-word and kill-phrase checks run on exactly what is left.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("N\u043e\u0445, mach das Licht aus.", "N, mach das Licht aus."),  # Cyrillic o, x
        ("\u041d\u041e\u0425 hilft.", "hilft."),  # upper-case Cyrillic "NOX"
        ("\u039d\u039f\u03a7 hilft.", "hilft."),  # upper-case Greek "NOX"
        ("\u043d\u043ex hilft.", "x hilft."),  # read "nox" by its sound
        ("\u03bd\u03bf\u03be hilft.", "hilft."),  # Greek "nox" by its sound
        ("\u0274\u1d0fx ist da.", "x ist da."),  # Latin small capitals (outside Latin A/B)
    ],
)
def test_no_letter_of_another_script_is_ever_said(text: str, expected: str) -> None:
    from nox.node.voice import latin_only, speakable

    assert latin_only(text) == expected
    spoken = speakable(text)
    assert spoken is None or all(ord(c) <= 0x024F for c in spoken)
    assert spoken is None or "nox" not in spoken.lower()


@pytest.mark.parametrize(
    "text",
    [
        "N\u200box hilft dir.",  # zero-width space
        "N\ufe0fox hilft dir.",  # variation selector
        "N\u034fox hilft dir.",  # combining grapheme joiner
        "No\u20dex hilft dir.",  # enclosing mark
        "\uff2e\uff4f\uff58 ist da.",  # fullwidth
    ],
)
def test_invisible_characters_cannot_hide_the_name(text: str) -> None:
    from nox.node.voice import latin_only, say_refusal, says_a_command, speakable

    assert latin_only(text).startswith(("Nox", "Nox "))
    assert says_a_command(text) and say_refusal(text) == "voice_say_command_words"
    spoken = speakable(text)
    assert spoken is not None and spoken.startswith("ich")


@pytest.mark.parametrize(
    "text", ["Sag Nox No\u200btaus.", "Gleich: not\u034f aus", "emergency\ufe0f stop"]
)
def test_a_kill_phrase_with_hidden_characters_is_dropped(text: str) -> None:
    from nox.node.voice import speakable

    assert speakable(text) is None


def test_a_kill_phrase_in_another_script_is_said_only_as_its_latin_remains() -> None:
    from nox.node.voice import speakable

    # A Cyrillic o: what is left ("Ntaus") is no kill phrase, and it is all that is said.
    assert speakable("Sag Nox N\u043etaus.") == "Sag ich Ntaus."


_ALLOWED_PUNCTUATION = set(".,;:!?'\"-\u2013\u2014()/%&+=@# ")


@pytest.mark.parametrize(
    "text",
    [
        "\u2115\U0001d560\U0001d569 hilft.",  # letterlike N + mathematical double-struck o, x
        "\u24dd\u24de\u24e7 hilft.",  # circled letters
        "\U0001f15d\U0001f15e\U0001f167 hilft.",  # negative circled capitals (no NFKC form)
        "\U0001f13d\U0001f13e\U0001f147 hilft.",  # squared capitals
        "\U0001f1f3\U0001f1f4\U0001f1fd hilft.",  # regional indicator letters (emoji)
        "\U0001d40d\U0001d428\U0001d431 hilft.",  # mathematical bold
        "\uff2e\uff4f\uff58 hilft.",  # fullwidth
        "N\u2605ox \u2135 \u2122 \u00b5 hilft.",  # a star inside the name, aleph, TM, micro
    ],
)
def test_only_an_allowlist_of_latin_text_is_ever_said(text: str) -> None:
    from nox.node.voice import latin_only, say_refusal, speakable

    kept = latin_only(text)
    assert all(c in _ALLOWED_PUNCTUATION or (ord(c) <= 0x024F and c.isalnum()) for c in kept), kept
    spoken = speakable(text)
    assert spoken is None or "nox" not in spoken.lower()
    # One result for the check and for the voice: what is said is the filtered text itself.
    if spoken is not None:
        assert spoken in (
            kept,
            kept.replace("Nox", "ich").replace("NOX", "ich").replace("nox", "ich"),
        )
    if "nox" in kept.lower():
        assert say_refusal(text) == "voice_say_command_words"


def test_ordinary_text_is_said_as_it_is_and_foreign_words_vanish() -> None:
    from nox.node.voice import say_refusal, says_a_command, speakable

    for text in ("Das Café in Springfield ist offen.", "Die Straße ist frei, Alex."):
        assert not says_a_command(text) and speakable(text) == text
    assert speakable("\u041f\u0440\u0438\u0432\u0435\u0442, Alex.") == ", Alex."
    assert speakable("\u041f\u0440\u0438\u0432\u0435\u0442!") is None  # nothing left to say
    assert say_refusal("\u041f\u0440\u0438\u0432\u0435\u0442!") == "voice_say_nothing_to_say"


async def test_the_node_filters_what_the_core_tells_it_to_say(tmp_path: Path) -> None:
    satellite, _seen = await _running(tmp_path)
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    turn = next(iter(satellite.turns))
    queue = satellite._queue  # noqa: SLF001
    assert queue is not None
    satellite._speaker.cancel()  # type: ignore[union-attr]  # noqa: SLF001 - inspect the queue
    await satellite.on_say({"turn_id": turn, "seq": 0, "text": "\u041d\u041e\u0425, hallo."})
    await satellite.on_say({"turn_id": turn, "seq": 1, "text": "Sag Nox Notaus."})
    await satellite.on_say({"turn_id": turn, "seq": 2, "text": "Bis gleich.", "last": True})
    said = [queue.get_nowait().text for _ in range(queue.qsize())]
    assert said == [", hallo.", "Bis gleich."]
    await satellite.shutdown()


def test_the_wake_word_matcher_folds_lookalikes_in_both_cases() -> None:
    from nox.voice.stt.wake_word import WakeWordMatcher, fold_confusables

    for heard in (
        "N\u043e\u0445 n\u043etaus",
        "\u039d\u039f\u03a7 NOTAUS",
        "N\u041e\u0425 notaus",
    ):
        match = WakeWordMatcher().match(heard)
        assert match.addressed and match.kill
    assert fold_confusables("\uff2e\u200b\ufe0f\u03bf\u0445") == "nox"
    assert fold_confusables("Straße") == "straße"  # German letters are left alone


# ---- this machine's own addresses (B4) ------------------------------------------------------


def test_unreadable_own_addresses_count_as_this_machine_during_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.node.agent import ADDRESSES_UNREADABLE, all_local_addresses
    from nox.node.world_routes import NodeControl
    from tests.unit.node.test_c6_followup import LAPTOP, _report

    def broken() -> Any:
        raise PermissionError("no access to the interface table")

    monkeypatch.setattr(psutil, "net_if_addrs", broken)
    assert all_local_addresses() == [ADDRESSES_UNREADABLE]  # never an empty list
    control = NodeControl(
        None,
        lambda _n: None,
        addresses=lambda: {LAPTOP: ("192.0.2.20", [])},  # some other address on the LAN
        own_addresses=all_local_addresses,
    )
    assert not control.controls_local()  # no session: nothing to protect
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    assert control.controls_local()  # cannot tell this machine apart: it is the controlled PC


def test_an_own_address_lookup_that_raises_fails_closed() -> None:
    from nox.node.world_routes import NodeControl
    from tests.unit.node.test_c6_followup import LAPTOP, _report

    def raising() -> list[str]:
        raise RuntimeError("psutil is gone")

    control = NodeControl(
        None,
        lambda _n: None,
        addresses=lambda: {LAPTOP: ("192.0.2.20", [])},
        own_addresses=raising,
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    assert control.controls_local()
