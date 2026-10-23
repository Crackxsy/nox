"""C9: the satellites - voice on a node, OBS where it runs, the pet overlay to a remote core.

Nothing here opens a microphone, plays a sound, reaches OBS or opens a window: every audio
device, speech engine, OBS connection and upstream WebSocket is a fake. All names and data are
fictitious ("Living room Pi", example.invalid).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from nox.core.events.names import E
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.tokens import TokenStore
from nox.lan.tls import TlsMaterial
from nox.node import protocol as p
from nox.node.gate import NodeGate
from nox.node.listener import NodeListener
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.obs import NodeObs, ObsSettings, obs_tools
from nox.node.overlay import PLACEHOLDER, OverlayRelay, local_origin, overlay_url, swap_token
from nox.node.pairing import NodePairing
from nox.node.policy import CORE_OPT_IN, NodePolicy, relevant_groups
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier
from nox.node.registry import ROUTABLE
from nox.node.repo import NodeRepository
from nox.node.router import origin_device, resolve
from nox.node.satellites import SatelliteHub, node_overlay_label, obs_route, voice_say_spec
from nox.node.voice import (
    MUTE_FILE,
    PTT_FILE,
    VoiceEngines,
    VoiceSatellite,
    VoiceSettings,
    voice_config,
    voice_tools,
)
from nox.security.model import Risk
from nox.security.taint import CONVERSATION
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.node.test_c4 import session_of, signed, start_node
from tests.unit.node.test_router import FakeRegistry, _entry

_NOW = datetime(2026, 10, 22, tzinfo=UTC)
PI = "a" * 32
PC = "b" * 32
WS_URL = "ws://127.0.0.1:47845/ws"
FAKE_PASSWORD = "fixture-obs-password"  # noqa: S105 - a test fixture, not a secret


# ---- fakes ---------------------------------------------------------------------------------------


class FakeIn:
    sample_rate = 16000

    def __init__(self) -> None:
        self.open = False
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def frames(self) -> AsyncIterator[np.ndarray]:
        await asyncio.Event().wait()  # no microphone: frames never come
        yield np.zeros(160, dtype=np.int16)

    @property
    def enabled(self) -> bool:
        return self.open

    async def set_enabled(self, value: bool) -> None:
        self.open = value


class FakeOut:
    level = 0.0

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []

    async def play(self, chunks: Any, rate: int, channel: Any, *, utterance_id: str) -> None:
        size = 0
        async for chunk in chunks:
            size += len(chunk)
        self.played.append((utterance_id, size))

    async def stop(self, *, reason: str) -> None:
        return None

    def devices(self) -> list[dict[str, str]]:
        return []


class FakeStt:
    id = "fake-stt"

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None


class FakeTts:
    id = "supertonic"
    sample_rate = 16000

    def __init__(self) -> None:
        self.said: list[str] = []
        self.unloaded = False

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        self.unloaded = True

    async def synthesize(self, request: Any) -> AsyncIterator[bytes]:
        self.said.append(request.text)
        yield b"\x00\x00" * 32


def _satellite(
    tmp_path: Path, *, allowed: bool = True, answer: dict[str, Any] | None = None
) -> tuple[VoiceSatellite, dict[str, Any]]:
    seen: dict[str, Any] = {"asked": [], "told": [], "stops": [], "audit": [], "loads": 0}
    engines = VoiceEngines(FakeIn(), FakeOut(), FakeStt(), FakeTts())
    seen["engines"] = engines
    flags = {"allowed": allowed}
    seen["flags"] = flags

    async def load(_settings: VoiceSettings, _home: Path) -> VoiceEngines:
        seen["loads"] += 1
        return engines

    async def ask(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        seen["asked"].append((method, params))
        return answer if answer is not None else {"result": {"accepted": True}}

    async def tell(method: str, params: dict[str, Any]) -> None:
        seen["told"].append((method, params))

    satellite = VoiceSatellite(
        home=tmp_path,
        settings=VoiceSettings,
        allowed=lambda: flags["allowed"],
        ask_core=ask,
        tell_core=tell,
        stop_here=seen["stops"].append,
        audit=lambda action, result: seen["audit"].append((action, result)),
        load=load,
    )
    return satellite, seen


async def _until(check: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


# ---- groups and catalogue ------------------------------------------------------------------------


def test_voice_and_obs_are_off_by_default_and_routable() -> None:
    defaults = NodeManifest()
    assert defaults.groups["voice"] is False and defaults.groups["obs"] is False
    assert {"voice.say", "obs.request"} <= ROUTABLE
    # The Devices page (C8) shows a group as soon as Nox routes a tool of it.
    assert {"voice", "obs"} <= relevant_groups()


def test_a_pi_gets_the_small_speech_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node import voice as voice_module

    monkeypatch.setattr(voice_module.platform, "machine", lambda: "aarch64")
    cfg = voice_config(VoiceSettings(), tmp_path)
    assert cfg.stt.model == "tiny" and cfg.tts.engine == "supertonic"
    assert cfg.stt.listening_mode == "ptt_only"  # push-to-talk unless the device says otherwise
    monkeypatch.setattr(voice_module.platform, "machine", lambda: "AMD64")
    assert voice_config(VoiceSettings(mode="wake_word"), tmp_path).stt.model == "base"
    assert voice_config(VoiceSettings(stt_model="small"), tmp_path).stt.model == "small"


# ---- the voice satellite on the node -------------------------------------------------------------


async def test_the_microphone_opens_only_when_everything_says_yes(tmp_path: Path) -> None:
    satellite, seen = _satellite(tmp_path)
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
    satellite, seen = _satellite(tmp_path)
    satellite.connection(True)
    await satellite.refresh()
    assert satellite.pipeline is not None
    seen["flags"]["allowed"] = False  # `nox node deny voice`, a stop, or narrowed by the core
    await satellite.refresh()
    assert satellite.pipeline is None and satellite.engines is None
    assert seen["engines"].tts.unloaded
    assert ("node.voice_off", "") in seen["audit"]


async def _running(tmp_path: Path, **kw: Any) -> tuple[VoiceSatellite, dict[str, Any]]:
    satellite, seen = _satellite(tmp_path, **kw)
    satellite.connection(True)
    await satellite.on_gate({"capture": True})
    await satellite.refresh()
    return satellite, seen


async def test_only_text_goes_to_the_core_and_nothing_of_it_is_kept(tmp_path: Path) -> None:
    satellite, seen = await _running(tmp_path)
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
    satellite, seen = await _running(tmp_path)
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
    await _until(lambda: len(tts.said) >= 2)
    await asyncio.sleep(0.05)
    assert tts.said == ["Hallo Alex.", "Bis gleich."]
    await satellite.shutdown()


async def test_a_new_question_or_a_stop_cancels_the_old_answer(tmp_path: Path) -> None:
    satellite, seen = await _running(tmp_path)
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
    satellite, seen = await _running(
        tmp_path, answer={"result": {"accepted": False, "reason": "microphone_off"}}
    )
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    assert not satellite.turns
    await satellite.shutdown()


async def test_the_kill_phrase_stops_the_node_and_never_reaches_the_core(tmp_path: Path) -> None:
    satellite, seen = await _running(tmp_path)
    await satellite._on_event(E.VOICE_KILL_PHRASE, {"by": "voice", "language": "de"})
    assert seen["stops"] and not seen["asked"]


async def test_only_the_loudness_goes_to_the_core_for_a_pet(tmp_path: Path) -> None:
    satellite, seen = await _running(tmp_path)
    await satellite._on_event(E.TTS_LEVEL, {"level": 0.42, "utterance_id": "u", "text": "x"})
    await satellite._on_event(E.TTS_LEVEL, {"level": 0.43})  # too soon after the last one
    await satellite._on_event(E.TTS_FINISHED, {"utterance_id": "u", "ok": True})
    levels = [params for method, params in seen["told"] if method == p.M_VOICE_LEVEL]
    assert levels[0] == {"level": 0.42, "turn_id": ""}
    assert levels[-1]["level"] == 0.0 and all(set(x) == {"level", "turn_id"} for x in levels)
    await satellite.shutdown()


# ---- the node's gate for the satellite tools -----------------------------------------------------


def _obs_world(tmp_path: Path) -> tuple[NodeObs, dict[str, Any]]:
    seen: dict[str, Any] = {"requests": [], "urls": []}

    class Client:
        connected = identified = True
        last_error = ""

        def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def request(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
            seen["requests"].append((kind, data or {}))
            if kind == "GetSceneList":
                return {
                    "scenes": [
                        {"sceneName": "Technical Problems", "sceneUuid": "u-privacy"},
                        {"sceneName": "Live", "sceneUuid": "u-live"},
                    ]
                }
            if kind == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": "Pause Card", "sourceUuid": "u-card"}]}
            if kind == "GetInputSettings":
                return {"inputSettings": {"url": "https://user:pw@example.invalid/x", "x": 1}}
            return {}

    def make(url: str, password: Any) -> Client:
        seen["urls"].append(url)
        seen["password"] = password
        return Client()

    obs = NodeObs(
        settings=lambda: ObsSettings(
            port=4455, privacy_scene="Technical Problems", scene_set=["Live"]
        ),
        password=lambda: FAKE_PASSWORD,
        make_client=make,
        panic_file=tmp_path / "obs-panic",
    )
    return obs, seen


def _gate(tools: dict[str, Any], groups: tuple[str, ...], keys: Path) -> tuple[NodeGate, Any]:
    signer = ReceiptSigner(keys / "receipts")
    manifest = NodeManifest(groups={g: g in groups for g in GROUPS})
    gate = NodeGate(tools, manifest, None)
    gate.receipts = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    return gate, signer


def _meta(signer: ReceiptSigner, name: str, args: dict[str, Any], risk: str) -> NoxCallMeta:
    meta = signed(signer, name, args, risk=risk, device_id="d" * 32, core_id="c" * 64)
    return NoxCallMeta.model_validate(meta)


def _req(kind: str, **data: Any) -> dict[str, Any]:
    return {"request_type": kind, "request_data": data}


def test_obs_on_a_node_follows_the_core_local_rules(tmp_path: Path) -> None:
    obs, _ = _obs_world(tmp_path)
    tools = obs_tools(obs)
    gate, signer = _gate(tools, ("obs",), tmp_path)
    name = "obs.request"
    # Stopping the stream and the stream key: never, whatever the core signed.
    for kind in ("StopStream", "GetStreamServiceSettings", "CallVendorRequest"):
        assert gate.precheck(name, _req(kind)) == "obs_request_never_possible"
    # The privacy scene by name: critical, refused before anyone is asked.
    assert gate.precheck(name, _req("RemoveScene", sceneName="Technical Problems")) is not None
    # A read is low here and still needs the core's receipt.
    read = _req("GetSceneList")
    assert gate.check(name, read).reason == "receipt_missing"
    decision = gate.check(name, read, meta=_meta(signer, name, read, "low"))
    assert decision.allowed and not decision.needs_local_confirmation
    # A change is medium: a core claiming less is refused.
    change = _req("SetCurrentProgramScene", sceneName="Live")
    assert gate.check(name, change, meta=_meta(signer, name, change, "low")).reason == (
        "risk_understated"
    )
    assert gate.check(name, change, meta=_meta(signer, name, change, "medium")).allowed
    # Going live is high: the person at this device says yes in the dialog.
    live = _req("StartStream")
    decision = gate.check(name, live, meta=_meta(signer, name, live, "high"))
    assert decision.allowed and decision.needs_local_confirmation
    # A switch outside this device's scene set is high as well.
    other = _req("SetCurrentProgramScene", sceneName="Gaming")
    assert gate.check(name, other, meta=_meta(signer, name, other, "medium")).reason == (
        "risk_understated"
    )


def test_obs_and_voice_need_their_group_on_the_device(tmp_path: Path) -> None:
    obs, _ = _obs_world(tmp_path)
    satellite, _ = _satellite(tmp_path)
    tools = {**obs_tools(obs), **voice_tools(satellite)}
    gate, signer = _gate(tools, ("system",), tmp_path)
    assert not gate.offered()
    args = _req("GetSceneList")
    meta = _meta(signer, "obs.request", args, "low")
    assert gate.check("obs.request", args, meta=meta).reason == "capability_disabled"
    say = {"text": "Das Essen ist fertig.", "language": "de"}
    meta = _meta(signer, "voice.say", say, "low")
    assert gate.check("voice.say", say, meta=meta).reason == "capability_disabled"
    gate.set_manifest(NodeManifest(groups={g: g == "voice" for g in GROUPS}))
    assert gate.check("voice.say", say).reason == "receipt_missing"
    assert gate.check("voice.say", say, meta=_meta(signer, "voice.say", say, "low")).allowed
    gate.narrow(["voice"])  # the core switched it off for this connection
    meta = _meta(signer, "voice.say", say, "low")
    assert gate.check("voice.say", say, meta=meta).reason == "capability_disabled"


async def test_obs_on_a_node_uses_loopback_its_own_password_and_redacts(tmp_path: Path) -> None:
    obs, seen = _obs_world(tmp_path)
    answer = await obs.request("GetInputSettings", {"inputName": "Browser"})
    assert seen["urls"] == ["ws://127.0.0.1:4455"]
    assert await seen["password"]() == FAKE_PASSWORD  # from this device's keyring only
    text = json.dumps(answer)
    assert FAKE_PASSWORD not in text and "user:pw" not in text and answer["ok"] is True


async def test_the_privacy_scene_and_its_contents_cannot_be_changed(tmp_path: Path) -> None:
    from nox.node.tools import NodeRefusal

    obs, seen = await _obs_ready(tmp_path)
    with pytest.raises(NodeRefusal) as refused:
        await obs.request("SetInputSettings", {"inputName": "Pause Card", "inputSettings": {}})
    assert refused.value.reason == "privacy_scene"
    assert ("SetInputSettings", {"inputName": "Pause Card", "inputSettings": {}}) not in seen[
        "requests"
    ]


async def _obs_ready(tmp_path: Path) -> tuple[NodeObs, dict[str, Any]]:
    obs, seen = _obs_world(tmp_path)
    return obs, seen


async def test_the_panic_button_switches_and_latches_until_this_device_resumes(
    tmp_path: Path,
) -> None:
    from nox.node.tools import NodeRefusal

    obs, seen = _obs_world(tmp_path)
    assert await obs.panic("panic") is True
    assert ("SetCurrentProgramScene", {"sceneName": "Technical Problems"}) in seen["requests"]
    with pytest.raises(NodeRefusal) as refused:
        await obs.request("SetCurrentProgramScene", {"sceneName": "Live"})
    assert refused.value.reason == "obs_panic"
    assert (await obs.request("GetSceneList", {}))["ok"] is True  # reads still work
    again, _ = _obs_world(tmp_path)  # a restart: the latch is in the node's folder
    assert again.panicked
    again.resume()
    assert not again.panicked and not (tmp_path / "obs-panic").exists()


# ---- the core: voice turns ----------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))


def _session(device_id: str = PI) -> Any:
    ready = asyncio.Event()
    ready.set()
    return SimpleNamespace(device_id=device_id, client=FakeClient(), ready=ready)


def _hub(
    *,
    offers: set[tuple[str, str]] | None = None,
    capture: str | None = None,
    turn: Any = None,
    sessions: list[Any] | None = None,
    overlay: Any = None,
    disconnected: list[list[str]] | None = None,
    opted_in: bool = True,
    may_hold: set[str] | None = None,
) -> tuple[SatelliteHub, dict[str, Any]]:
    seen: dict[str, Any] = {"published": [], "turns": [], "audit": []}
    allowed = offers if offers is not None else {(PI, "voice.say"), (PI, "obs.request")}

    async def default_turn(text: str, language: str, on_chunk: Any, channel: str) -> None:
        from nox.memory.capture import _TURN_ORIGIN

        seen["turns"].append((text, language, channel, _TURN_ORIGIN.get()))
        for part in ("Es ist ", "zwölf Uhr. ", "Noch etwas?"):
            await on_chunk(part)

    async def disconnect(ids: list[str]) -> int:
        if disconnected is not None:
            disconnected.append(ids)
        return len(ids)

    audit = SimpleNamespace(append=lambda **kw: seen["audit"].append(kw))
    hub = SatelliteHub(
        offers=lambda device, tool: (device, tool) in allowed,
        enabled_on_device=lambda device, group: (
            group == "obs" and (device, "obs.request") in allowed
        ),
        sessions=lambda: sessions or [],
        turn=turn or default_turn,
        capture_refusal=lambda: capture,
        publish=lambda name, payload: seen["published"].append((name, payload)),
        audit=audit,  # type: ignore[arg-type]
        overlay=overlay,
        disconnect_credential=disconnect,
        opted_in=lambda _device, _group: opted_in,
        overlay_allowed=lambda device: device in (may_hold if may_hold is not None else {PI}),
    )
    return hub, seen


def _heard(text: str = "Wie spät ist es?") -> p.VoiceHeard:
    return p.VoiceHeard(turn_id="e" * 32, text=text, language="de")


async def test_the_core_answers_a_node_sentence_by_sentence(tmp_path: Path) -> None:
    hub, seen = _hub()
    session = _session()
    assert await hub.handle(session, p.M_VOICE_HEARD, _heard()) == {"accepted": True}
    await _until(lambda: any(m[1].get("last") for m in session.client.sent))
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
    session = _session()
    hub, _ = _hub(offers=set())
    assert (await hub.handle(session, p.M_VOICE_HEARD, _heard()))["reason"] == "voice_not_enabled"
    hub, _ = _hub(capture="microphone off")
    assert (await hub.handle(session, p.M_VOICE_HEARD, _heard()))["reason"] == "microphone_off"
    hub, _ = _hub()
    answers = [await hub.handle(session, p.M_VOICE_HEARD, _heard()) for _ in range(14)]
    assert answers[-1] == {"accepted": False, "reason": "too_many"}
    await hub.close()


async def test_a_failed_turn_is_said_and_a_new_question_cancels_the_old() -> None:
    gate = asyncio.Event()

    async def slow(text: str, language: str, on_chunk: Any, channel: str) -> None:
        if text == "boom":
            raise RuntimeError("model down")
        await gate.wait()

    hub, _ = _hub(turn=slow)
    session = _session()
    await hub.handle(session, p.M_VOICE_HEARD, _heard("erste"))
    await asyncio.sleep(0.01)
    await hub.handle(session, p.M_VOICE_HEARD, _heard("boom"))
    await _until(lambda: any(m == p.M_VOICE_CANCEL for m, _ in session.client.sent))
    await _until(lambda: any(params.get("last") for _m, params in session.client.sent))
    last = [params for m, params in session.client.sent if m == p.M_VOICE_SAY][-1]
    assert last["text"] == "Das hat nicht funktioniert." and last["last"] is True
    await hub.close()


async def test_a_nodes_speech_level_is_published_for_its_own_pet() -> None:
    hub, seen = _hub()
    await hub.handle(_session(), p.M_VOICE_LEVEL, p.VoiceLevel(level=0.3))
    name, payload = seen["published"][0]
    assert name == "tts.level" and payload["device"] == PI and payload["level"] == 0.3
    hub, seen = _hub(offers=set())
    await hub.handle(_session(), p.M_VOICE_LEVEL, p.VoiceLevel(level=0.3))
    assert not seen["published"]


async def test_the_microphone_gate_follows_the_cores_privacy() -> None:
    session = _session()
    state = {"why": None}
    hub, _ = _hub(sessions=[session])
    hub._capture_refusal = lambda: state["why"]  # type: ignore[method-assign]
    await hub.push_gates()
    await hub.push_gates()  # unchanged: said once
    state["why"] = "microphone off (panic)"
    await hub.push_gates()
    gates = [params for method, params in session.client.sent if method == p.M_VOICE_GATE]
    assert [g["capture"] for g in gates] == [True, False]


async def test_the_panic_button_reaches_obs_on_every_node_with_obs() -> None:
    pi, pc = _session(PI), _session(PC)
    hub, _ = _hub(sessions=[pi, pc])
    assert await hub.panic() == 1
    assert pi.client.sent == [(p.M_OBS_PANIC, {"reason": "panic"})] and not pc.client.sent


# ---- the core: routing --------------------------------------------------------------------------


def test_here_at_a_voice_satellite_means_that_node() -> None:
    token = CONVERSATION.set(f"voice:node:{PI}#abc")
    try:
        assert origin_device() == f"node:{PI}"
        entries = [
            _entry(PI, "Living room Pi", tools={"media.control": "low"}),
            _entry(PC, "Work PC", tools={"media.control": "low"}),
        ]
        for requested, default in (("hier", None), (None, "origin")):
            found = resolve(
                "media.control",
                requested,
                entries=entries,
                core_has=True,
                side_effects=True,
                default=default,
                origin=origin_device(),
            )
            assert found.device == PI
    finally:
        CONVERSATION.reset(token)


def _obs_spec(registry: FakeRegistry, own: ToolSpec | None = None) -> ToolSpec:
    spec: ToolSpec = obs_route(registry, dict)(own)
    return spec


def test_obs_request_on_a_node_is_classified_like_on_the_core() -> None:
    pc = _entry(PC, "Stream PC", tools={"obs.request": "low"})
    pc.confirmer = True
    spec = _obs_spec(FakeRegistry([pc]))

    def scope(kind: str, **data: Any) -> Any:
        payload = spec.input_model.model_validate(
            {"request_type": kind, "request_data": data, "device": "Stream PC"}
        ).model_dump()
        return spec.scope(payload), payload  # type: ignore[misc]

    read, _ = scope("GetSceneList")
    assert read.risk is Risk.LOW and read.action == "request.GetSceneList"
    change, _ = scope("SetInputMute", inputName="Mic", inputMuted=True)
    assert change.risk is Risk.MEDIUM and change.action == "request.SetInputMute"
    # The core does not know that device's scene set: every switch is asked on the device.
    switch, _ = scope("SetCurrentProgramScene", sceneName="Live")
    assert switch.risk is Risk.HIGH and switch.confirm_on_device
    delete, _ = scope("RemoveScene", sceneName="Live")
    assert delete.action == "scene.delete"  # the shipped profiles' rules still apply
    never, payload = scope("StopStream")
    assert never.target == "device:none"
    assert asyncio.run(spec.handler(payload))["error"] == "obs_request_never_possible"


def test_the_route_wraps_the_plugins_tool_as_it_comes_and_goes() -> None:
    from pydantic import BaseModel

    class Plugin(BaseModel):
        request_type: str
        request_data: dict[str, Any] = {}

    async def plugin_handler(payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "here": True}

    plugin = ToolSpec(
        name="obs.request",
        description="Send one obs-websocket request.",
        input_model=Plugin,
        risk=Risk.HIGH,
        handler=plugin_handler,
        side_effects=True,
    )
    tools = ToolRegistry()
    tools.wrap("obs.request", obs_route(FakeRegistry([]), dict))
    assert tools.own("obs.request") is None and tools.get("obs.request") is not None
    tools.register(plugin)  # the plugin starts later: it becomes "on this core"
    assert tools.own("obs.request") is plugin
    payload = (
        tools.get("obs.request")
        .input_model.model_validate(  # type: ignore[union-attr]
            {"request_type": "GetSceneList", "device": "core"}
        )
        .model_dump()
    )
    spec = tools.get("obs.request")
    assert spec is not None and spec.scope is not None
    spec.scope(payload)
    assert asyncio.run(spec.handler(payload)) == {"ok": True, "here": True}
    with pytest.raises(ValueError, match="already registered"):
        tools.register(plugin)
    tools.unregister("obs.request")  # the plugin stops: the route stays, for nodes only
    assert tools.own("obs.request") is None and "obs.request" in tools


def test_voice_say_is_low_and_shows_its_text_in_full() -> None:
    pi = _entry(PI, "Living room Pi", tools={"voice.say": "low"})
    spec = voice_say_spec(FakeRegistry([pi]), dict)
    payload = spec.input_model.model_validate(
        {"text": "Das Essen ist fertig.", "device": "Living room Pi"}
    ).model_dump()
    scope = spec.scope(payload)  # type: ignore[misc]
    assert scope.risk is Risk.LOW and '"Das Essen ist fertig."' in scope.target


# ---- the core: two keys for a microphone ---------------------------------------------------------


async def test_voice_needs_the_owners_second_key_on_every_listing(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from tests.unit.node.conftest import pair_node

    assert "voice" in CORE_OPT_IN
    device_id = (await pair_node(pairing, tmp_path)).node_id
    policy = NodePolicy(nodes)
    tools = {"voice.say": "low", "system.info": "read"}
    # The very first listing already reports voice enabled: narrowed, nothing routed.
    routed = policy.record_listing(device_id, tools, reported={"voice", "system"}, complete=True)
    assert "voice.say" not in routed and "voice" in policy.narrowed(device_id)
    assert not policy.opted_in(device_id, "voice")
    # Even if the narrowing was lost (a reset table, a race): narrowed again, every time.
    nodes.narrow(device_id, "voice", False, when=_NOW)
    routed = policy.record_listing(device_id, tools, reported={"voice", "system"})
    assert "voice.say" not in routed
    assert "voice" in policy.narrowed(device_id)
    # The owner clears it in the Devices page (PIN): the second key, kept across reconnects.
    policy.set_narrowed(device_id, "voice", False)
    assert policy.opted_in(device_id, "voice")
    for _ in range(2):
        routed = policy.record_listing(
            device_id, tools, reported={"voice", "system"}, complete=True
        )
        assert "voice.say" in routed
    # Narrowing it again takes the key back.
    policy.set_narrowed(device_id, "voice", True)
    assert not policy.opted_in(device_id, "voice")
    policy.set_narrowed(device_id, "voice", False)
    # Removing the node clears it.
    assert await pairing.revoke(device_id, reason="test")
    assert not policy.opted_in(device_id, "voice")


async def test_no_utterance_without_the_second_key() -> None:
    session = _session()
    hub, _ = _hub(opted_in=False)
    answer = await hub.handle(session, p.M_VOICE_HEARD, _heard())
    assert answer == {"accepted": False, "reason": "voice_not_switched_on_here"}

    def broken(_device: str, _group: str) -> bool:
        raise RuntimeError("database is gone")

    hub, _ = _hub()
    hub._opted_in = broken  # type: ignore[method-assign]
    answer = await hub.handle(session, p.M_VOICE_HEARD, _heard())
    assert answer == {"accepted": False, "reason": "voice_not_switched_on_here"}  # fail closed


# ---- the overlay relay --------------------------------------------------------------------------


def _auth(role: str = "overlay", token: str = PLACEHOLDER) -> str:
    return json.dumps(
        {
            "v": 1,
            "id": "1",
            "kind": "request",
            "name": "ipc.auth",
            "src": {"role": role, "id": f"{role}:x"},
            "payload": {"token": token, "role": role, "id": f"{role}:x"},
        }
    )


def test_the_relay_swaps_in_its_credential_for_an_overlay_only() -> None:
    swapped = swap_token(_auth(), "t" * 43)
    assert swapped is not None and json.loads(swapped)["payload"]["token"] == "t" * 43
    assert swap_token(_auth("device"), "t" * 43) is None
    assert swap_token(_auth("dashboard"), "t" * 43) is None
    assert swap_token("not json", "t" * 43) is None
    assert local_origin(None, 47845) and local_origin("http://127.0.0.1:47845", 47845)
    assert not local_origin("https://example.invalid", 47845)
    url = overlay_url(47845, PI)
    assert url.endswith(f"&device={PI}#overlay={PLACEHOLDER}") and "127.0.0.1" in url


def _relay(
    tmp_path: Path,
    *,
    allowed: bool = True,
    token: str | None = "n" * 43,
    ca_file: Path | None = None,
) -> tuple[OverlayRelay, dict[str, Any]]:
    from nox.node.home import NodeIdentity

    seen: dict[str, Any] = {"upstream": [], "credentials": 0, "fetched": []}
    identity = NodeIdentity(
        node_id=PI,
        name="Living room Pi",
        core_host="nox-core.example.invalid",
        core_port=47843,
        node_port=47844,
        ca_fingerprint="f" * 64,
        paired_at=datetime(2026, 10, 22, tzinfo=UTC),
    )

    async def credential() -> str | None:
        seen["credentials"] += 1
        return token

    class Upstream:
        close_code = 1000

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

        def __aiter__(self) -> Upstream:
            self._done = False
            return self

        async def __anext__(self) -> str:
            if self._done:
                await asyncio.sleep(3600)
            self._done = True
            return json.dumps({"kind": "response", "name": "ipc.auth", "payload": {"ok": True}})

        async def close(self) -> None:
            return None

    async def connect(url: str, **kw: Any) -> Upstream:
        upstream = Upstream()
        seen["upstream"].append((url, kw, upstream))
        return upstream

    async def fetch(path: str) -> tuple[int, str, bytes]:
        seen["fetched"].append(path)
        return 200, "text/html", b"<html>pet</html>"

    relay = OverlayRelay(
        identity=lambda: identity,
        ca_file=ca_file or tmp_path / "ca.pem",
        allowed=lambda: allowed,
        credential=credential,
        fetch=fetch,
        connect=connect,
    )
    return relay, seen


def test_the_relay_serves_the_page_on_loopback_only(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    relay, seen = _relay(tmp_path)
    client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
    page = client.get("/pet/index.html")
    assert page.status_code == 200 and seen["fetched"] == ["/pet/index.html"]
    assert "ws://127.0.0.1:47845" in page.headers["content-security-policy"]
    assert client.get("/health").json()["ws_port"] == 47845
    assert client.get("/pet/../secret", follow_redirects=False).status_code in (403, 404)
    rebinding = TestClient(relay.app(), base_url="http://evil.example.invalid:47845")
    assert rebinding.get("/pet/index.html").status_code == 403
    assert seen["fetched"] == ["/pet/index.html"]


def test_the_relay_carries_only_an_overlay_with_the_nodes_credential(
    tmp_path: Path, tls: TlsMaterial
) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    relay, seen = _relay(tmp_path, ca_file=tls.ca_file)  # TLS pinned to this authority only
    client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
    with client.websocket_connect(WS_URL) as ws:
        ws.send_text(_auth())
        reply = json.loads(ws.receive_text())
    assert reply["payload"]["ok"] is True
    url, kw, upstream = seen["upstream"][0]
    assert url == "wss://nox-core.example.invalid:47843/ws"
    assert kw["server_hostname"] == "nox-core.example.invalid"
    assert json.loads(upstream.sent[0])["payload"]["token"] == "n" * 43  # never the placeholder
    with pytest.raises(WebSocketDisconnect):  # not an overlay: refused
        with client.websocket_connect(WS_URL) as ws:
            ws.send_text(_auth("device"))
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):  # a web page elsewhere
        with client.websocket_connect(WS_URL, headers={"origin": "https://example.invalid"}):
            pass
    assert len(seen["upstream"]) == 1


def test_offline_or_switched_off_the_page_is_closed_and_retries(tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    for relay, seen in (_relay(tmp_path, allowed=False), _relay(tmp_path, token=None)):
        client = TestClient(relay.app(), base_url="http://127.0.0.1:47845")
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(WS_URL) as ws:
                ws.send_text(_auth())
                ws.receive_text()
        assert closed.value.code == 1013 and not seen["upstream"]


# ---- the core: overlay credentials --------------------------------------------------------------


def _tokens(store: OverlayTokenStore, may_hold: set[str]) -> TokenStore:
    tokens = TokenStore(overlay=store)
    tokens.node_overlay_allowed = lambda device: device in may_hold
    return tokens


def _lan(tokens: TokenStore, token: str, role: str = "overlay") -> bool:
    return tokens.authenticate(token, role, f"{role}:x", via_lan=True).ok


async def test_a_node_with_obs_gets_a_read_only_rotating_credential(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    disconnected: list[list[str]] = []
    hub, _ = _hub(overlay=store, disconnected=disconnected)
    first = await hub.handle(_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert first is not None and first["ok"]
    may_hold = {PI}
    tokens = _tokens(store, may_hold)
    # Through the network listener: only a node's overlay credential, and only as `overlay`.
    assert _lan(tokens, first["token"])
    assert not _lan(tokens, first["token"], "dashboard")
    _obs, obs_token = store.issue("obs")
    assert not _lan(tokens, obs_token)
    assert tokens.authenticate(obs_token, "overlay", "overlay:x").ok  # OBS on the core itself
    # The hub asks again at every use: a node that may not hold one now gets nothing.
    may_hold.clear()
    assert not _lan(tokens, first["token"])
    assert not _lan(TokenStore(overlay=store), first["token"])  # no check installed: no
    tokens.node_overlay_allowed = lambda _device: 1 / 0  # type: ignore[assignment,return-value]
    assert not _lan(tokens, first["token"])  # a failing check: no
    may_hold.add(PI)
    tokens = _tokens(store, may_hold)
    again = await hub.handle(_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert again == {"ok": False, "reason": "too_soon"}
    hub._nodes[PI].credential_at -= 120  # a minute later: a new one replaces the old
    second = await hub.handle(_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert second is not None and second["ok"] and disconnected
    assert not _lan(tokens, first["token"])
    assert await hub.revoke_overlay(PI, "node removed") == 1
    assert not _lan(tokens, second["token"])
    assert node_overlay_label(PI) not in {c.label for c in store.credentials()}


async def test_no_obs_no_credential(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    hub, _ = _hub(overlay=store, offers={(PI, "voice.say")})
    answer = await hub.handle(_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert answer == {"ok": False, "reason": "obs_not_enabled"}
    hub, _ = _hub(overlay=store, may_hold=set())  # offered, but narrowed meanwhile
    answer = await hub.handle(_session(), p.M_OVERLAY_CREDENTIAL, p.OverlayCredentialRequest())
    assert answer == {"ok": False, "reason": "obs_not_enabled"} and not store.credentials()


async def test_a_restarted_core_sweeps_stale_node_credentials(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    store.issue(node_overlay_label(PI))  # from an earlier run: PI may still hold it
    store.issue(node_overlay_label(PC))  # from an earlier run: PC was narrowed, offline, removed
    store.issue("obs")  # the core's own OBS overlay: not a node's, never swept
    reopened = OverlayTokenStore(tmp_path / "overlay_tokens.json")  # the restart
    hub, _ = _hub(overlay=reopened, may_hold={PI})
    assert await hub.sweep() == 1
    assert {c.label for c in reopened.credentials()} == {node_overlay_label(PI), "obs"}
    hub, _ = _hub(overlay=reopened, may_hold={PI})
    hub._overlay_allowed = lambda _device: 1 / 0  # type: ignore[assignment,return-value]
    assert await hub.sweep() == 1  # cannot tell: revoked (fail closed)
    assert {c.label for c in reopened.credentials()} == {"obs"}


async def test_narrowing_obs_or_removing_the_node_revokes_its_overlay_while_offline(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from nox.node.registry import NodeRegistry
    from tests.unit.node.conftest import pair_node

    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    registry = NodeRegistry(pairing, nodes)
    hub, _ = _hub(overlay=store)

    async def narrowed(device_id: str, group: str, on: bool) -> None:
        if group == "obs" and on:
            await hub.revoke_overlay(device_id, "obs narrowed")

    async def removed(device_id: str, reason: str) -> None:
        await hub.revoke_overlay(device_id, reason)

    registry.on_narrowed.append(narrowed)
    pairing.on_revoked(removed)
    laptop = (await pair_node(pairing, tmp_path, name="Stream PC")).node_id
    store.issue(node_overlay_label(laptop))
    await registry.set_narrowed(laptop, "obs", True)  # the node is not connected
    assert not store.credentials()
    store.issue(node_overlay_label(laptop))
    assert await pairing.revoke(laptop, reason="test")
    assert not store.credentials()


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
        await _until(lambda: bool(node.agent.features))
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
        await _until(lambda: bool(said))
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
        await _until(lambda: node.agent.connected.is_set())
        assert node.agent.features == frozenset()
        heard = {"turn_id": "e" * 32, "text": "Hallo", "language": "de"}
        assert await node.agent.ask_core(p.M_VOICE_HEARD, heard) is None
        assert not await node.agent.notify_core(p.M_VOICE_LEVEL, {"level": 0.1})
        assert session.violations == 0
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 10)
