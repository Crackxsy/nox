"""Screen and camera captures from a node: this core's privacy decides before a node is
asked, the device shows a notice and asks, the picture travels as blob chunks, is checked
against its hash and stored on the core."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.device.tools import NodeRefusal
from nox.node.device.world import world_tools
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.registry import BLOB_RESULT_KEY
from nox.node.hub.world_routes import capture_refusal
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.security.model import Risk
from tests.unit.node import support
from tests.unit.node.support import Confirmer, call, session_of, start_node
from tests.unit.node.support_sessions import (
    PNG,
    device_world,
    privacy_state,
    route_call,
    routed_specs,
)

# ---- captures ------------------------------------------------------------------------------------


async def test_a_capture_shows_a_notice_first_and_keeps_nothing() -> None:
    world, seen = device_world()
    tools = world_tools(world)
    result = await tools["camera.snapshot"].handler(tools["camera.snapshot"].args())
    assert seen["notices"] == ["Nox is taking a camera picture now (asked for by the Nox at home)."]
    assert result["_blob"] == PNG and result["sha256"] == hashlib.sha256(PNG).hexdigest()


async def test_no_visible_notice_no_picture() -> None:
    world, seen = device_world(notice_fails=True)
    tools = world_tools(world)
    for name in ("screen.capture", "camera.snapshot"):
        with pytest.raises(NodeRefusal) as refused:
            await tools[name].handler(tools[name].args())
        assert refused.value.reason == "no_visible_indicator"
    assert seen["grabs"] == 0


async def test_a_capture_travels_as_blob_chunks_and_needs_the_device_dialog(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, seen = device_world()
    big = PNG + b"x" * (600 * 1024)  # more than one chunk
    world.screen = lambda: _value(big)
    monkeypatch.setattr(support, "scripted_tools", lambda _ran: world_tools(world))
    confirmer = Confirmer("allow")
    manifest = NodeManifest(groups={g: g in ("system", "screen") for g in GROUPS})
    node = await start_node(
        listener, pairing, tls, tmp_path, confirmer=confirmer, manifest=manifest
    )
    try:
        session = await session_of(listener, node.node_id)
        node.agent.session_report = world.session_report
        answer = await call(
            session, listener.signer, "screen.capture", {}, risk="high", call_id="cap1"
        )  # type: ignore[attr-defined]
        assert answer["_nox"]["session"] == world.session_report()  # every answer says it
        assert confirmer.asked and "screen.capture" in confirmer.asked[0]  # asked on the device
        assert "_blob" not in answer["structuredContent"]
        assert answer["structuredContent"]["sha256"] == hashlib.sha256(big).hexdigest()
        assert session.take_blob("cap1") == big
        confirmer.answer = "deny"
        denied = await call(session, listener.signer, "screen.capture", {}, risk="high")  # type: ignore[attr-defined]
        assert denied["_nox"]["refused"] == "denied_on_device"
        assert denied["_nox"]["session"]["active"] is False  # refusals too
        assert len(seen["notices"]) == 1  # no notice, no picture for the refused one
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def _value(data: bytes) -> bytes:
    return data


def test_this_cores_privacy_decides_before_a_node_is_asked() -> None:
    assert capture_refusal("screen", privacy_state()) is None
    assert "camera capture is off" in str(capture_refusal("camera", privacy_state()))
    assert "private" in str(capture_refusal("screen", privacy_state("private")))
    assert capture_refusal("screen", None) is not None


async def test_a_node_capture_is_stored_on_this_core_and_the_model_gets_the_path(
    tmp_path: Path,
) -> None:
    sent: list[dict[str, Any]] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        sent.append(kw)
        meta = {"ok": True, "sha256": hashlib.sha256(PNG).hexdigest()}
        return {"device": entry.label, "result": meta, BLOB_RESULT_KEY: PNG}

    specs, _reg, _control, _ = routed_specs(tmp_path, privacy_state(), answer)
    spec = specs["screen.capture"]
    payload = spec.input_model.model_validate({"device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.HIGH and scope.confirm_on_device and scope.rememberable is False
    result = await spec.handler(payload)
    assert result["ok"] and sent[0]["blob"] is True and BLOB_RESULT_KEY not in result
    path = Path(result["path"])
    assert path.parent == tmp_path / "captures" / "screen"
    assert await asyncio.to_thread(path.read_bytes) == PNG
    assert result["cloud_allowed"] is False  # BALANCED keeps screenshots local


async def test_a_capture_that_does_not_match_its_hash_or_privacy_is_refused(tmp_path: Path) -> None:
    calls: list[str] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(tool)
        return {"device": entry.label, "result": {"ok": True, "sha256": "0" * 64}, "_blob": PNG}

    specs, _reg, _control, _ = routed_specs(tmp_path, privacy_state(), answer)
    for name, error in (
        ("screen.capture", "malformed_result"),
        ("camera.snapshot", "capture.refused"),
    ):
        spec = specs[name]
        payload = spec.input_model.model_validate({"device": "Laptop"}).model_dump()
        spec.scope(payload)
        assert (await spec.handler(payload))["error"] == error
    assert calls == ["screen.capture"]  # the camera was never asked: off in this core's privacy
    assert not (tmp_path / "captures").exists() or not any((tmp_path / "captures").rglob("*.png"))


# ---- M5 and the captures -------------------------------------------------------------------------


async def test_privacy_switched_on_during_the_dialog_discards_the_picture(tmp_path: Path) -> None:
    privacy = privacy_state()

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        # The person at the device takes their time; meanwhile panic mode switches capture off.
        privacy.allows_capture = lambda _kind: False
        meta = {"ok": True, "sha256": hashlib.sha256(PNG).hexdigest()}
        return {"device": entry.label, "result": meta, BLOB_RESULT_KEY: PNG}

    specs, *_ = routed_specs(tmp_path, privacy, answer)
    result = await route_call(specs, "screen.capture", {})
    assert result["error"] == "capture.refused"
    assert not (tmp_path / "captures").exists() or not any((tmp_path / "captures").rglob("*.png"))


async def test_a_large_screen_is_made_smaller_on_the_node() -> None:
    world, _ = device_world()
    huge = PNG + b"x" * (9 * 1024 * 1024)
    world.screen = lambda: _bytes(huge)
    world.fit = lambda _png: PNG + b"small"
    tools = world_tools(world)
    result = await tools["screen.capture"].handler(tools["screen.capture"].args())
    assert result["_blob"] == PNG + b"small"
    world.fit = lambda _png: None  # cannot be made smaller: refused, never cut
    result = await tools["screen.capture"].handler(tools["screen.capture"].args())
    assert result == {"ok": False, "error": "capture.too_large", "bytes": len(huge)}


async def _bytes(data: bytes) -> bytes:
    return data


async def test_window_titles_from_a_node_are_hidden_in_panic(tmp_path: Path) -> None:
    privacy = privacy_state()
    privacy.panic = False
    privacy.snapshot = lambda: SimpleNamespace(safe_mode=False)
    window = {"handle": 7, "title": "Bank - Kontostand", "process": "browser.exe"}

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return {"device": entry.label, "result": {"ok": True, "windows": [dict(window)]}}

    laptop_tools = {"desktop.windows": "read"}
    specs, registry, *_ = routed_specs(tmp_path, privacy, answer)
    registry._entries[0].tools.update(laptop_tools)  # type: ignore[attr-defined]
    shown = await route_call(specs, "desktop.windows", {})
    assert shown["result"]["windows"][0]["title"] == "Bank - Kontostand"
    privacy.panic = True
    hidden = await route_call(specs, "desktop.windows", {})
    assert hidden["result"]["windows"][0]["title"] == "[hidden: privacy]"
