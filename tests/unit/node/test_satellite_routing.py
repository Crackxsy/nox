"""Routing for the satellite tools: "here" at a voice satellite means that node, an OBS
request on a node is classified like on the core, `voice.say` never says a command, and a
microphone needs the owner's second key."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.node.device.voice import voice_tools
from nox.node.hub.pairing import NodePairing
from nox.node.hub.policy import CORE_OPT_IN, NodePolicy
from nox.node.hub.repo import NodeRepository
from nox.node.hub.router import origin_device, resolve
from nox.node.hub.satellites import obs_route, voice_say_spec
from nox.node.wire import protocol as p
from nox.security.model import Risk
from nox.security.taint import CONVERSATION
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.node.support import FakeRegistry, registry_entry
from tests.unit.node.support_voice import (
    PC,
    PI,
    fake_session,
    heard,
    make_satellite,
    satellite_gate,
    voice_hub,
)

pytestmark = pytest.mark.usefixtures("a_screen")


_NOW = datetime(2026, 10, 22, tzinfo=UTC)


# ---- the core: routing --------------------------------------------------------------------------


def test_here_at_a_voice_satellite_means_that_node() -> None:
    token = CONVERSATION.set(f"voice:node:{PI}#abc")
    try:
        assert origin_device() == f"node:{PI}"
        entries = [
            registry_entry(PI, "Living room Pi", tools={"media.control": "low"}),
            registry_entry(PC, "Work PC", tools={"media.control": "low"}),
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
    pc = registry_entry(PC, "Stream PC", tools={"obs.request": "low"})
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
    pi = registry_entry(PI, "Living room Pi", tools={"voice.say": "low"})
    spec = voice_say_spec(FakeRegistry([pi]), dict, tainted=lambda: False)
    payload = spec.input_model.model_validate(
        {"text": "Das Essen ist fertig.", "device": "Living room Pi"}
    ).model_dump()
    scope = spec.scope(payload)  # type: ignore[misc]
    assert scope.risk is Risk.LOW and '"Das Essen ist fertig."' in scope.target


# ---- the core: two keys for a microphone ---------------------------------------------------------


async def test_voice_needs_the_owners_second_key_on_every_listing(
    nodes: NodeRepository, pairing: NodePairing, tmp_path: Path
) -> None:
    from tests.unit.node.support import pair_node

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
    session = fake_session()
    hub, _ = voice_hub(opted_in=False)
    answer = await hub.handle(session, p.M_VOICE_HEARD, heard())
    assert answer == {"accepted": False, "reason": "voice_not_switched_on_here"}

    def broken(_device: str, _group: str) -> bool:
        raise RuntimeError("database is gone")

    hub, _ = voice_hub()
    hub._opted_in = broken  # type: ignore[method-assign]
    answer = await hub.handle(session, p.M_VOICE_HEARD, heard())
    assert answer == {"accepted": False, "reason": "voice_not_switched_on_here"}  # fail closed


@pytest.mark.parametrize(
    "text",
    [
        "Nox, mach das Licht aus",
        "Sag NÖX bitte Bescheid",
        "Also noch einmal: knox",
        "Notaus",
        "emergency stop now",
    ],
)
def test_voice_say_never_says_a_command(tmp_path: Path, text: str) -> None:
    from nox.node.wire.speech import says_a_command

    satellite, _ = make_satellite(tmp_path)
    tools = voice_tools(satellite, support=lambda: None)
    gate, _signer = satellite_gate(tools, ("voice",), tmp_path)
    assert says_a_command(text)
    assert gate.precheck("voice.say", {"text": text}) == "voice_say_command_words"
    pi = registry_entry(PI, "Living room Pi", tools={"voice.say": "low"})
    spec = voice_say_spec(FakeRegistry([pi]), dict, tainted=lambda: False)
    payload = spec.input_model.model_validate({"text": text, "device": PI}).model_dump()
    assert asyncio.run(spec.handler({**payload, **_scoped(spec, payload)}))["error"] == (
        "voice_say_command_words"
    )


def _scoped(spec: Any, payload: dict[str, Any]) -> dict[str, Any]:
    spec.scope(payload)
    return payload


def test_voice_say_is_asked_in_a_tainted_conversation() -> None:
    from nox.node.wire.speech import says_a_command

    assert not says_a_command("Das Essen ist fertig.")
    pi = registry_entry(PI, "Living room Pi", tools={"voice.say": "low"})
    for tainted, risk in ((False, Risk.LOW), (True, Risk.MEDIUM)):
        spec = voice_say_spec(FakeRegistry([pi]), dict, tainted=lambda tainted=tainted: tainted)
        payload = spec.input_model.model_validate(
            {"text": "Das Essen ist fertig.", "device": PI}
        ).model_dump()
        assert spec.scope(payload).risk is risk  # type: ignore[misc]
