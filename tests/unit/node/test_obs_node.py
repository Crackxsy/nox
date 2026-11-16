"""OBS on a node: the core's rules for a request apply unchanged, the node uses loopback and
its own password, the privacy scene cannot be changed, and the panic button latches until
this device resumes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.ipc.tokens import TokenStore
from nox.node import protocol as p
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.obs import NodeObs, ObsSettings, obs_tools
from nox.node.pairing import NodePairing
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner
from nox.node.repo import NodeRepository
from nox.node.satellites import node_overlay_label
from nox.node.voice import (
    voice_tools,
)
from tests.unit.node.support import signed
from tests.unit.node.support_voice import (
    PI,
    fake_session,
    lan_accepts,
    make_satellite,
    satellite_gate,
)

pytestmark = pytest.mark.usefixtures("a_screen")


FAKE_PASSWORD = "fixture-obs-password"  # noqa: S105 - a test fixture, not a secret


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


def _meta(signer: ReceiptSigner, name: str, args: dict[str, Any], risk: str) -> NoxCallMeta:
    meta = signed(signer, name, args, risk=risk, device_id="d" * 32, core_id="c" * 64)
    return NoxCallMeta.model_validate(meta)


def _req(kind: str, **data: Any) -> dict[str, Any]:
    return {"request_type": kind, "request_data": data}


def test_obs_on_a_node_follows_the_core_local_rules(tmp_path: Path) -> None:
    obs, _ = _obs_world(tmp_path)
    tools = obs_tools(obs)
    gate, signer = satellite_gate(tools, ("obs",), tmp_path)
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
    satellite, _ = make_satellite(tmp_path)
    tools = {**obs_tools(obs), **voice_tools(satellite, support=lambda: None)}
    gate, signer = satellite_gate(tools, ("system",), tmp_path)
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


def test_only_a_node_label_passes_even_with_a_permissive_check(tmp_path: Path) -> None:
    store = OverlayTokenStore(tmp_path / "overlay_tokens.json")
    tokens = TokenStore(overlay=store)
    tokens.node_overlay_allowed = lambda _device: True
    _obs, obs_token = store.issue("obs")
    _x, odd_token = store.issue("nodes:" + PI)
    assert not lan_accepts(tokens, obs_token) and not lan_accepts(tokens, odd_token)
    _n, node_token = store.issue(node_overlay_label(PI))
    decision = tokens.authenticate(node_token, "overlay", "overlay:x", via_lan=True)
    assert decision.ok and decision.overlay_node == PI


async def test_a_never_possible_request_reaches_no_obs(tmp_path: Path) -> None:
    from nox.node.tools import NodeRefusal

    obs, seen = _obs_world(tmp_path)
    for kind in ("StopStream", "GetStreamServiceSettings"):
        with pytest.raises(NodeRefusal) as refused:
            await obs.request(kind, {})
        assert refused.value.reason == "obs_request_never_possible"
    assert not seen["requests"] and not seen["urls"]


async def test_resume_in_a_terminal_lifts_the_latch_of_the_running_node(tmp_path: Path) -> None:
    from nox.node.home import NodeHome
    from nox.node.satellite_cli import obs_resume

    home = NodeHome(tmp_path)
    home.ensure()
    obs, _seen = _obs_world(home.path)
    await obs.panic("panic")
    assert obs.panicked
    obs_resume(home)  # the CLI, on the same device, while the node runs
    answer = await obs.request("SetCurrentProgramScene", {"sceneName": "Live"})
    assert answer["ok"] is True


async def test_the_record_folder_stays_on_the_node(tmp_path: Path) -> None:
    obs, _ = _obs_world(tmp_path)

    async def record_dir(kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"recordDirectory": "C:\\Users\\Alex\\Videos\\Streams"}

    client = await obs.client()
    client.request = record_dir
    answer = await obs.request("GetRecordDirectory", {})
    assert answer["response"]["recordDirectory"] == "Streams"
    assert "Alex" not in json.dumps(answer)


async def test_a_node_connecting_during_a_panic_gets_it(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from nox.node.install import NodeRuntime, install_satellites
    from nox.node.registry import NodeRegistry
    from tests.unit.node.support import pair_node

    registry = NodeRegistry(pairing, nodes)
    stream_pc = (await pair_node(pairing, tmp_path, name="Stream PC")).node_id
    entry = registry._entry(stream_pc, "Stream PC")
    entry.online, entry.tools = True, {"obs.request": "low"}
    session = fake_session(stream_pc)
    listener = SimpleNamespace(
        sessions={stream_pc: session}, _on_connected=None, _on_disconnected=None
    )
    privacy = SimpleNamespace(panic=True, allows_capture=lambda _kind: False)
    core = SimpleNamespace(
        security=SimpleNamespace(privacy=privacy, killswitch=None, audit=None, engine=None),
        orchestrator=None,
        tokens=None,
        hub=None,
        bus=None,
    )
    runtime = NodeRuntime(pairing=pairing, enabled=True)
    install_satellites(core, runtime, registry, listener, pairing)
    await listener._on_connected(session)
    assert (p.M_OBS_PANIC, {"reason": "panic"}) in session.client.sent
    await runtime.stop()


async def test_an_empty_record_folder_is_hidden(tmp_path: Path) -> None:
    obs, _ = _obs_world(tmp_path)

    async def record_dir(kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"recordDirectory": ""}

    client = await obs.client()
    client.request = record_dir
    answer = await obs.request("GetRecordDirectory", {})
    assert answer["response"]["recordDirectory"] == "[hidden]"
