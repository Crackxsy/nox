"""Follow-ups to C6: talking to Nox during a node session, switching the microphone back on, and
the wiring the review's surviving mutants pointed at. Fakes only - nothing real is captured,
sent or switched."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from nox.node.world_routes import REFUSE_LINKED, REFUSE_ROLE, NodeControl, session_refusal
from tests.unit.node.test_c6 import BROWSER, LAPTOP, _report
from tests.unit.node.test_c6_review import OTHER_PHONE, Gate, _ctx, _envelope, _wired
from tests.unit.node.test_router import _entry

LOCAL, PHONE, LINKED = "dashboard:1", "device:" + OTHER_PHONE, "device:" + BROWSER


class Mute(BaseModel):
    muted: bool


class Text(BaseModel):
    text: str = "hallo"


def _registry(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, NodeControl, list[str]]:
    import nox.node.install as install_module

    monkeypatch.setattr(install_module, "console_is_remote", lambda: False)
    core, control, _guard, _audit = _wired(Gate())
    ran: list[str] = []

    def handler(name: str) -> Any:
        async def run(_ctx: Any, _p: Any) -> dict[str, Any]:
            ran.append(name)
            if name == "chat.send":  # the answer's fixed shape (`protocol.ChatSendResult`)
                return {"request_id": "r", "text": "ok", "provider": "x", "degraded": False}
            return {}

        return run

    roles = ("dashboard", "shell", "device")
    core.registry.register("chat.send", Text, handler("chat.send"), roles=roles)
    core.registry.register("voice.ptt", Text, handler("voice.ptt"), roles=roles)
    core.registry.register("voice.mute", Mute, handler("voice.mute"), roles=roles)
    core.registry.register("plans.approve", Text, handler("plans.approve"), roles=roles)
    return core, control, ran


async def _send(core: Any, name: str, client: str, payload: dict[str, Any]) -> bool:
    from nox.ipc.dispatch import RequestContext
    from nox.ipc.protocol import Kind

    role = "device" if client.startswith("device:") else "dashboard"
    envelope = _envelope(name, role, client, payload)
    reply = await core.registry.dispatch(
        RequestContext(client_id=client, role=role, request=envelope), envelope
    )
    return bool(reply.kind is Kind.RESPONSE)


async def test_talking_to_nox_needs_no_pin_except_from_the_controlled_pc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _control, ran = _registry(monkeypatch)
    for name in ("chat.send", "voice.ptt"):
        assert await _send(core, name, LOCAL, {}) is True
        assert await _send(core, name, PHONE, {}) is True
        # The browser on the PC Nox controls: never, with or without a PIN - else Nox could type
        # its own instructions.
        assert await _send(core, name, LINKED, {}) is False
        assert await _send(core, name, LINKED, {"pin": "4711"}) is False
    assert ran == ["chat.send", "chat.send", "voice.ptt", "voice.ptt"]
    # What a conversation leads to still follows the PIN rule.
    assert await _send(core, "plans.approve", LOCAL, {}) is False
    assert await _send(core, "plans.approve", PHONE, {}) is False


async def test_muting_is_free_and_unmuting_follows_the_pin_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, control, ran = _registry(monkeypatch)
    assert await _send(core, "voice.mute", LINKED, {"muted": True}) is True  # muting: always
    assert await _send(core, "voice.mute", LOCAL, {"muted": False}) is False
    assert await _send(core, "voice.mute", LINKED, {"muted": False, "pin": "4711"}) is False
    assert await _send(core, "voice.mute", LOCAL, {"muted": False, "pin": "4711"}) is True
    assert await _send(core, "voice.mute", PHONE, {"muted": False, "pin": "4711"}) is True
    assert ran == ["voice.mute"] * 3
    control.observe(LAPTOP, _report(3, False))  # the session is over: nothing asked
    assert await _send(core, "voice.mute", LINKED, {"muted": False}) is True


def test_the_calm_rule_for_one_call() -> None:
    from nox.ipc.dispatch import calm_call, conversation_request

    assert calm_call("voice.mute", {"muted": True}) and not calm_call(
        "voice.mute", {"muted": False}
    )
    assert calm_call("security.kill", {}) and calm_call("security.permission.pending", {})
    assert not calm_call("chat.send", {}) and conversation_request("chat.send")
    assert conversation_request("voice.ptt") and not conversation_request("plans.approve")


async def test_a_paired_device_without_a_pin_check_is_refused() -> None:
    """S6: no device PIN guard on this core - refused, never let through."""
    control = NodeControl(None, lambda _n: None, linked=lambda _d: None)
    control.observe(LAPTOP, _report(2, True))
    refusal = await session_refusal(
        control,
        _ctx("device", PHONE),
        "4711",
        action="plans.approve",
        gate=Gate(),
        device_guard=None,
        remote=lambda: False,
    )
    assert refusal == REFUSE_ROLE


async def test_the_installed_link_lookup_finds_the_controlled_browser() -> None:
    """W3: the `linked()` that `_world_specs` installs reads the registry's links."""
    from nox.ipc.dispatch import RequestContext, RequestRegistry
    from nox.node.install import _world_specs
    from nox.tools.registry import ToolRegistry

    laptop = _entry(LAPTOP, "Laptop", linked=BROWSER)

    class Registry:
        def known(self) -> list[Any]:
            return [laptop]

        def entries(self) -> list[Any]:
            return [laptop]

    core = SimpleNamespace(
        registry=RequestRegistry(),
        tool_registry=ToolRegistry(),
        config=SimpleNamespace(),
        security=SimpleNamespace(gate=Gate()),
        device_pin_guard=None,
    )
    _specs, control = _world_specs(core, Registry(), dict, [])  # type: ignore[arg-type]
    assert not control.controls_browser(BROWSER)
    control.observe(LAPTOP, _report(2, True))
    assert control.controls_browser(BROWSER) and not control.controls_browser(OTHER_PHONE)
    status = core.registry.get("node.computer.status")
    assert status is not None

    async def ask(client: str, role: str) -> Any:
        ctx = RequestContext(
            client_id=client, role=role, request=_envelope("node.computer.status", role, client, {})
        )
        return await status.handler(ctx, status.payload_model())

    assert (await ask(LINKED, "device"))["controlled_here"] is True
    assert (await ask(PHONE, "device"))["controlled_here"] is False
    assert (await ask(LOCAL, "dashboard"))["controlled_here"] is False
    assert REFUSE_LINKED  # what the linked browser is told elsewhere
