"""The voice satellite on a node: the microphone opens only when everything says yes, only
text goes to the core, the answer is spoken only for a question asked here, and the kill
phrase stops the node without reaching the core. No audio device is opened."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nox.core.events.names import E
from nox.lan.tls import TlsMaterial
from nox.node.device.voice import MUTE_FILE, PTT_FILE, VoiceSettings, voice_config, voice_tools
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.policy import relevant_groups
from nox.node.hub.registry import ROUTABLE
from nox.node.wire import protocol as p
from nox.node.wire.manifest import NodeManifest
from tests.unit.fakes import wait_until
from tests.unit.node.support import session_of, start_node
from tests.unit.node.support_voice import (
    FakeIn,
    FakeOut,
    FakeTts,
    make_satellite,
    running_satellite,
)

pytestmark = pytest.mark.usefixtures("a_screen")


# ---- groups and catalogue ------------------------------------------------------------------------


def test_voice_and_obs_are_off_by_default_and_routable() -> None:
    defaults = NodeManifest()
    assert defaults.groups["voice"] is False and defaults.groups["obs"] is False
    assert {"voice.say", "obs.request"} <= ROUTABLE
    # The Devices page shows a group as soon as Nox routes a tool of it.
    assert {"voice", "obs"} <= relevant_groups()


def test_a_pi_gets_the_small_speech_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node.device import voice as voice_module

    monkeypatch.setattr(voice_module.platform, "machine", lambda: "aarch64")
    cfg = voice_config(VoiceSettings(), tmp_path)
    assert cfg.stt.model == "tiny" and cfg.tts.engine == "supertonic"
    assert cfg.stt.listening_mode == "ptt_only"  # push-to-talk unless the device says otherwise
    monkeypatch.setattr(voice_module.platform, "machine", lambda: "AMD64")
    assert voice_config(VoiceSettings(mode="wake_word"), tmp_path).stt.model == "base"
    assert voice_config(VoiceSettings(stt_model="small"), tmp_path).stt.model == "small"


def test_a_headless_device_listens_for_the_wake_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push-to-talk needs a key to hold - a tray or a terminal. A
    device without a screen listens for the wake word unless its settings say otherwise."""
    from nox.node.device.voice import listening_mode

    assert VoiceSettings().mode == "auto"
    assert listening_mode(VoiceSettings(), screen=lambda: False) == "wake_word"
    assert listening_mode(VoiceSettings(), screen=lambda: True) == "push_to_talk"
    assert listening_mode(VoiceSettings(mode="push_to_talk"), screen=lambda: False) == (
        "push_to_talk"
    )
    monkeypatch.setattr("nox.node.device.confirm.has_screen", lambda: False)
    assert voice_config(VoiceSettings(), tmp_path).stt.listening_mode == "continuous"


def test_voice_say_is_offered_only_where_the_voice_libraries_are(tmp_path: Path) -> None:
    from nox.node.device.voice import voice_support

    satellite, _seen = make_satellite(tmp_path)
    assert voice_tools(satellite, support=lambda: "the extra voice is missing") == {}
    assert "voice.say" in voice_tools(satellite, support=lambda: None)
    assert voice_support(lambda module: module != "sounddevice") is not None
    assert voice_support(lambda module: module != "supertonic") is None  # Piper speaks then
    assert voice_support(lambda module: module not in ("supertonic", "piper")) is not None
    assert voice_support(lambda _module: True) is None


def test_voice_setup_asks_first_and_downloads_into_the_node_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The models only after a yes, into the node's own folder - and
    the node's speech recognition never downloads by itself (`local_files_only`)."""
    from nox.node.device import voice as voice_module
    from nox.voice.stt.faster_whisper_engine import FasterWhisperStt

    monkeypatch.setattr(voice_module, "voice_support", lambda: None)
    fetched: list[tuple[str, Path]] = []
    said: list[str] = []

    def whisper(model: str, target: Path) -> None:
        fetched.append((model, target))

    def tts(engine: str, target: Path, _echo: Any) -> int:
        fetched.append((engine, target))
        return 0

    kw: dict[str, Any] = {"echo": said.append, "download_whisper": whisper, "download_tts": tts}
    refused = voice_module.voice_setup(tmp_path, VoiceSettings(), confirm=lambda _q: False, **kw)
    assert refused == 1 and fetched == [] and "Nothing was downloaded." in said
    questions: list[str] = []
    done = voice_module.voice_setup(
        tmp_path, VoiceSettings(), confirm=lambda q: questions.append(q) or True, **kw
    )
    assert done == 0 and "Hugging Face" in questions[0]
    assert [name for name, _ in fetched] == [
        "base" if not voice_module.small_device() else "tiny",
        "supertonic",
    ]
    assert all(str(target).startswith(str(tmp_path / "models")) for _, target in fetched)
    assert FasterWhisperStt().local_files_only is False  # the core keeps its own behaviour
    assert FasterWhisperStt(local_files_only=True).local_files_only is True


# ---- the voice satellite on the node -------------------------------------------------------------


async def test_the_microphone_opens_only_when_everything_says_yes(tmp_path: Path) -> None:
    satellite, seen = make_satellite(tmp_path)
    mic: FakeIn = seen["engines"].audio_in
    satellite.connection(True)
    await satellite.refresh()
    assert satellite.pipeline is not None  # the group is usable: the engines are loaded
    assert not mic.open  # the core has not allowed the microphone yet (default: closed)
    await satellite.on_gate({"capture": True})
    (tmp_path / PTT_FILE).write_text("now")
    await satellite.refresh()
    assert mic.open  # push-to-talk held, the core allows it, not muted
    assert ("earcon", 2 * int(16000 * 90 / 1000)) in seen["engines"].audio_out.played
    (tmp_path / MUTE_FILE).write_text("muted")
    await satellite.refresh()
    assert not mic.open and satellite.state()["muted"]
    (tmp_path / MUTE_FILE).unlink()
    await satellite.on_gate({"capture": False, "reason": "panic"})
    await satellite.refresh()
    assert not mic.open
    await satellite.on_gate({"capture": True})
    satellite.connection(False)  # no core: nothing to tell, so nothing heard
    await satellite.refresh()
    assert satellite.pipeline is None and not mic.open
    await satellite.shutdown()


async def test_switching_the_group_off_frees_the_models(tmp_path: Path) -> None:
    satellite, seen = make_satellite(tmp_path)
    satellite.connection(True)
    await satellite.refresh()
    assert satellite.pipeline is not None
    seen["flags"]["allowed"] = False  # `nox node deny voice`, a stop, or narrowed by the core
    await satellite.refresh()
    assert satellite.pipeline is None and satellite.engines is None
    assert seen["engines"].tts.unloaded
    assert ("node.voice_off", "") in seen["audit"]


async def test_only_text_goes_to_the_core_and_nothing_of_it_is_kept(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY,
        {"text": "Wie spät ist es in Springfield?", "language": "de", "addressed_to_nox": True},
    )
    assert len(seen["asked"]) == 1
    method, params = seen["asked"][0]
    assert method == p.M_VOICE_HEARD
    assert set(params) == {"turn_id", "text", "language"}  # text only: never audio
    assert params["text"] == "Wie spät ist es in Springfield?"
    audit = " ".join(result for _action, result in seen["audit"])
    assert "Springfield" not in audit and "chars" in audit
    assert "Springfield" not in json.dumps(satellite.state())
    # Not addressed (no wake word, no push-to-talk): never sent.
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "nebenbei", "addressed_to_nox": False}
    )
    assert len(seen["asked"]) == 1
    await satellite.shutdown()


async def test_the_answer_is_spoken_only_for_a_question_asked_here(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    turn = seen["asked"][0][1]["turn_id"]
    tts: FakeTts = seen["engines"].tts
    await satellite.on_say({"turn_id": "c" * 32, "seq": 0, "text": "Fremd.", "last": True})
    await satellite.on_say({"turn_id": turn, "seq": 0, "text": "Hallo Alex."})
    await satellite.on_say({"turn_id": turn, "seq": 0, "text": "Noch einmal."})  # replayed
    await satellite.on_say({"turn_id": turn, "seq": 1, "text": "Bis gleich.", "last": True})
    await satellite.on_say({"turn_id": turn, "seq": 2, "text": "Nach dem Ende."})
    await wait_until(lambda: len(tts.said) >= 2, what="the second sentence was never spoken")
    await asyncio.sleep(0.05)
    assert tts.said == ["Hallo Alex.", "Bis gleich."]
    from nox.voice.base import Channel

    out: FakeOut = seen["engines"].audio_out
    # This device's own speaker, never a stream channel.
    assert out.channels and all(c is Channel.PRIVATE for c in out.channels)
    await satellite.shutdown()


async def test_a_new_question_or_a_stop_cancels_the_old_answer(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    for text in ("Erste Frage", "Zweite Frage"):
        await satellite._on_event(
            E.VOICE_TRANSCRIPT_READY, {"text": text, "language": "de", "addressed_to_nox": True}
        )
    first, second = (params["turn_id"] for _m, params in seen["asked"])
    assert first not in satellite.turns and second in satellite.turns
    satellite.stopped("kill")
    assert not satellite.turns
    await satellite.shutdown()


async def test_a_refused_question_is_forgotten(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(
        tmp_path, answer={"result": {"accepted": False, "reason": "microphone_off"}}
    )
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    assert not satellite.turns
    await satellite.shutdown()


async def test_a_refusal_is_remembered_in_words_and_sounds_once(tmp_path: Path) -> None:
    """Review of #195 (UX): a core's "no" must not be silent - the reason goes to voice.json for
    `nox node voice status`, a low two-tone "no" plays, and not again within the rate limit."""
    import json

    from nox.node.device.satellite_cli import voice_status
    from nox.node.device.voice import REFUSAL_SOUND_EVERY_S, STATE_FILE

    satellite, seen = await running_satellite(
        tmp_path, answer={"result": {"accepted": False, "reason": "voice_not_switched_on_here"}}
    )
    out = seen["engines"].audio_out
    out.played.clear()
    heard = {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert out.played == [("earcon", 4 * int(16000 * 140 / 1000))]  # two tones, no real sound
    state = json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8"))
    assert state["last_refusal"]["reason"] == "voice_not_switched_on_here"
    assert state["last_refusal"]["at"]
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert len(out.played) == 1  # rate-limited: the reason is updated, the sound is not repeated
    satellite.clock = lambda c=satellite.clock: c() + REFUSAL_SOUND_EVERY_S + 1  # type: ignore[assignment]
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert len(out.played) == 2
    # The status says it in words, in both languages.
    from nox.node.device.home import NodeHome

    home = NodeHome(tmp_path)
    for lang, words in (
        ("en", "Not switched on here yet"),
        ("de", "Hier noch nicht eingeschaltet"),
    ):
        import os

        os.environ["NOX_LANG"] = lang
        try:
            lines = voice_status(home)
        finally:
            del os.environ["NOX_LANG"]
        assert any(words in line for line in lines), lines
        assert not any("voice_not_switched_on_here" in line for line in lines)
    # An accepted question clears it.
    satellite.ask_core = lambda *_a: _accepted()  # type: ignore[assignment]
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert satellite.last_refusal is None
    await satellite.shutdown()


async def _accepted() -> dict[str, Any]:
    return {"result": {"accepted": True}}


def test_every_refusal_the_core_gives_has_words() -> None:
    from nox.node.device.satellite_cli import refusal_words

    for code in (
        "voice_not_switched_on_here",
        "voice_not_enabled",
        "controlled_here",
        "microphone_off",
        "too_many",
        "no_assistant",
        "echo",
    ):
        assert code not in refusal_words(code)
    assert "strange_code" in refusal_words("strange_code")  # an unknown one is shown as it is


def test_whisper_is_fetched_by_pinned_revision() -> None:
    from nox.node.device.voice import WHISPER_REVISIONS

    assert set(WHISPER_REVISIONS) == {"tiny", "base", "small"}
    assert all(
        len(r) == 40 and set(r) <= set("0123456789abcdef") for r in WHISPER_REVISIONS.values()
    )


async def test_the_kill_phrase_stops_the_node_and_never_reaches_the_core(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    await satellite._on_event(E.VOICE_KILL_PHRASE, {"by": "voice", "language": "de"})
    assert seen["stops"] and not seen["asked"]


async def test_only_the_loudness_goes_to_the_core_for_a_pet(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    await satellite._on_event(E.TTS_LEVEL, {"level": 0.42, "utterance_id": "u", "text": "x"})
    await satellite._on_event(E.TTS_LEVEL, {"level": 0.43})  # too soon after the last one
    await satellite._on_event(E.TTS_FINISHED, {"utterance_id": "u", "ok": True})
    levels = [params for method, params in seen["told"] if method == p.M_VOICE_LEVEL]
    assert levels[0] == {"level": 0.42, "turn_id": ""}
    assert levels[-1]["level"] == 0.0 and all(set(x) == {"level", "turn_id"} for x in levels)
    await satellite.shutdown()


# ---- the channel: features and satellite methods over real TLS -----------------------------


async def test_satellite_methods_travel_only_to_a_core_that_understands_them(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    got: list[tuple[str, Any]] = []

    async def on_satellite(session: Any, method: str, params: Any) -> dict[str, Any] | None:
        got.append((method, params))
        return {"accepted": True} if method == p.M_VOICE_HEARD else None

    listener.on_satellite = on_satellite
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        await wait_until(
            lambda: bool(node.agent.features),
            what="the node never learned which features the core has",
        )
        assert p.FEATURE_VOICE in node.agent.features
        heard = {"turn_id": "e" * 32, "text": "Hallo Nox", "language": "de"}
        answer = await node.agent.ask_core(p.M_VOICE_HEARD, heard)
        assert answer is not None and answer["result"] == {"accepted": True}
        assert got[0][0] == p.M_VOICE_HEARD and got[0][1].text == "Hallo Nox"
        said: list[dict[str, Any]] = []

        async def on_say(params: dict[str, Any]) -> None:
            said.append(params)

        node.agent.satellites[p.M_VOICE_SAY] = on_say
        await session.client.notify(p.M_VOICE_SAY, {"turn_id": "e" * 32, "seq": 0, "text": "Hi"})
        await wait_until(lambda: bool(said), what="the node's satellite handler was never called")
        assert said[0]["text"] == "Hi"
        assert session.violations == 0
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 10)


async def test_an_older_core_is_sent_no_satellite_method(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        await wait_until(lambda: node.agent.connected.is_set(), what="the node never connected")
        assert node.agent.features == frozenset()
        heard = {"turn_id": "e" * 32, "text": "Hallo", "language": "de"}
        assert await node.agent.ask_core(p.M_VOICE_HEARD, heard) is None
        assert not await node.agent.notify_core(p.M_VOICE_LEVEL, {"level": 0.1})
        assert session.violations == 0
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 10)


# ---- while it speaks, per wake word, and when it may not listen ----------------------------------


async def test_while_the_node_speaks_only_the_kill_phrase_counts(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    clock = {"now": 100.0}
    satellite.clock = lambda: clock["now"]
    heard = {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    satellite.pipeline._speaking = True  # noqa: SLF001 - the pipeline plays an answer
    assert satellite.capture_allowed()  # open: the kill phrase must stay reachable
    await satellite._on_event(E.VOICE_INPUT_STARTED, {"forced": False})
    satellite.pipeline._speaking = False  # noqa: SLF001
    await satellite._on_event(E.TTS_FINISHED, {"utterance_id": "u", "ok": True})
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)  # its own echo
    assert not seen["asked"]
    clock["now"] += 0.31
    await satellite._on_event(E.VOICE_INPUT_STARTED, {"forced": False})
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert len(seen["asked"]) == 1  # after the tail: a question again
    satellite.pipeline._speaking = True  # noqa: SLF001
    await satellite._on_event(E.VOICE_KILL_PHRASE, {"by": "voice", "language": "de"})
    assert seen["stops"]  # "Nox, Notaus" works during a long answer
    satellite._ptt_held = True  # push-to-talk while it speaks: a person's deliberate act
    await satellite._on_event(E.VOICE_INPUT_STARTED, {"forced": True})
    satellite.pipeline._speaking = False  # noqa: SLF001
    clock["now"] += 1.0
    await satellite._on_event(E.VOICE_TRANSCRIPT_READY, heard)
    assert len(seen["asked"]) == 2
    assert satellite.pipeline.config.barge_in is False  # no barge-in on voice activity alone
    await satellite.shutdown()


async def test_one_utterance_per_wake_word(tmp_path: Path) -> None:
    satellite, _ = await running_satellite(tmp_path)
    gate = satellite.pipeline.wake_gate
    gate.note_addressed()  # the pipeline opens a follow-up window when Nox was addressed
    assert gate.conversation_open
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    assert not gate.conversation_open  # consumed: the next one needs the wake word again
    assert voice_config(VoiceSettings(), tmp_path).stt.conversation_window_s == 0.0
    await satellite.shutdown()


async def test_nothing_is_sent_while_the_microphone_may_not_listen(tmp_path: Path) -> None:
    satellite, seen = await running_satellite(tmp_path)
    await satellite.on_gate({"capture": False, "reason": "panic"})
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    assert not seen["asked"]
    await satellite.shutdown()
