"""Talking to Nox while it controls a node: no PIN except from the controlled PC, muting is
free and unmuting follows the PIN rule, and a paired device's message during a session
counts as untrusted."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.world_routes import REFUSE_LINKED, REFUSE_ROLE, NodeControl, session_refusal
from tests.unit.node.support import registry_entry
from tests.unit.node.support_sessions import (
    BROWSER,
    LAPTOP,
    LINKED,
    LOCAL,
    OTHER_PHONE,
    PHONE,
    PinGate,
    conversation_registry,
    request_ctx,
    request_envelope,
    send_from,
    session_report,
    wired_core,
)


async def _send(core: Any, name: str, client: str, payload: dict[str, Any]) -> bool:
    from nox.ipc.dispatch import RequestContext
    from nox.ipc.protocol import Kind

    role = "device" if client.startswith("device:") else "dashboard"
    envelope = request_envelope(name, role, client, payload)
    reply = await core.registry.dispatch(
        RequestContext(client_id=client, role=role, request=envelope), envelope
    )
    return bool(reply.kind is Kind.RESPONSE)


async def test_talking_to_nox_needs_no_pin_except_from_the_controlled_pc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, _control, ran = conversation_registry(monkeypatch)
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
    core, control, ran = conversation_registry(monkeypatch)
    assert await _send(core, "voice.mute", LINKED, {"muted": True}) is True  # muting: always
    assert await _send(core, "voice.mute", LOCAL, {"muted": False}) is False
    assert await _send(core, "voice.mute", LINKED, {"muted": False, "pin": "4711"}) is False
    assert await _send(core, "voice.mute", LOCAL, {"muted": False, "pin": "4711"}) is True
    assert await _send(core, "voice.mute", PHONE, {"muted": False, "pin": "4711"}) is True
    assert ran == ["voice.mute"] * 3
    control.observe(LAPTOP, session_report(3, False))  # the session is over: nothing asked
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
    control.answered(LAPTOP, session_report(2, True))
    refusal = await session_refusal(
        control,
        request_ctx("device", PHONE),
        "4711",
        action="plans.approve",
        gate=PinGate(),
        device_guard=None,
        remote=lambda: False,
    )
    assert refusal == REFUSE_ROLE


async def test_the_installed_link_lookup_finds_the_controlled_browser() -> None:
    """W3: the `linked()` that `_world_specs` installs reads the registry's links."""
    from nox.ipc.dispatch import RequestContext, RequestRegistry
    from nox.node.install import _world_specs
    from nox.tools.registry import ToolRegistry

    laptop = registry_entry(LAPTOP, "Laptop", linked=BROWSER)

    class Registry:
        def known(self) -> list[Any]:
            return [laptop]

        def entries(self) -> list[Any]:
            return [laptop]

    core = SimpleNamespace(
        registry=RequestRegistry(),
        tool_registry=ToolRegistry(),
        config=SimpleNamespace(),
        security=SimpleNamespace(gate=PinGate()),
        device_pin_guard=None,
    )
    _specs, control = _world_specs(core, Registry(), dict, [])  # type: ignore[arg-type]
    assert not control.controls_browser(BROWSER)
    control.answered(LAPTOP, session_report(2, True))
    assert control.controls_browser(BROWSER) and not control.controls_browser(OTHER_PHONE)
    status = core.registry.get("node.computer.status")
    assert status is not None

    async def ask(client: str, role: str) -> Any:
        ctx = RequestContext(
            client_id=client,
            role=role,
            request=request_envelope("node.computer.status", role, client, {}),
        )
        return await status.handler(ctx, status.payload_model())

    assert (await ask(LINKED, "device"))["controlled_here"] is True
    assert (await ask(PHONE, "device"))["controlled_here"] is False
    assert (await ask(LOCAL, "dashboard"))["controlled_here"] is False
    assert REFUSE_LINKED  # what the linked browser is told elsewhere


@pytest.mark.parametrize("muted", ["false", 0, "no", None, "", [], {}])
async def test_anything_but_a_literal_true_is_not_muting(
    monkeypatch: pytest.MonkeyPatch, muted: Any
) -> None:
    import nox.node.install as install_module
    from nox.ipc.dispatch import calm_call
    from nox.ipc.handlers.core import VoiceMute
    from nox.ipc.protocol import Kind

    monkeypatch.setattr(install_module, "console_is_remote", lambda: False)
    core, _control, _guard, _audit = wired_core(PinGate())
    ran: list[Any] = []

    async def handler(_ctx: Any, p: Any) -> dict[str, Any]:
        ran.append(p.muted)
        return {}

    core.registry.register("voice.mute", VoiceMute, handler, roles=("dashboard", "device"))
    assert not calm_call("voice.mute", {"muted": muted})
    refused = await send_from(core, "voice.mute", LOCAL, {"muted": muted}, "")
    assert refused.kind is Kind.ERROR and ran == []  # the PIN rule, not the calm one
    # With the PIN the strict model still refuses a string or a number - never an "unmute".
    with_pin = await send_from(core, "voice.mute", LOCAL, {"muted": muted, "pin": "4711"}, "")
    assert with_pin.kind is Kind.ERROR and ran == []
    assert (await send_from(core, "voice.mute", LINKED, {"muted": True}, "")).kind is Kind.RESPONSE
    assert ran == [True]


async def test_talking_from_this_machine_keeps_the_remote_check() -> None:
    control = NodeControl(None, lambda _n: None)
    control.answered(LAPTOP, session_report(2, True))

    async def ask(remote: bool | None) -> str | None:
        return await session_refusal(
            control,
            request_ctx("dashboard", LOCAL),
            None,
            action="chat.send",
            gate=PinGate(),
            device_guard=None,
            remote=lambda: remote,
        )

    from nox.node.world_routes import REFUSE_REMOTE

    assert await ask(False) is None  # no PIN needed for talking ...
    assert await ask(True) == REFUSE_REMOTE  # ... but never through a remote desktop
    assert await ask(None) == REFUSE_REMOTE


async def test_a_browser_on_the_controlled_pc_counts_as_linked_by_its_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.ipc.protocol import Kind

    core, control, ran = conversation_registry(monkeypatch)
    control._addresses = lambda: {LAPTOP: ("192.0.2.10", ["fe80::1%eth0"])}  # type: ignore[assignment]
    control.pull()
    # An unlinked phone ... on the controlled PC's address: like the linked browser.
    for peer in ("192.0.2.10", "::ffff:192.0.2.10", "[fe80::1]"):
        reply = await send_from(core, "chat.send", PHONE, {}, peer)
        assert reply.kind is Kind.ERROR and reply.payload["message"] == REFUSE_LINKED, peer
    assert ran == []
    # ... from another address: talking works.
    assert (await send_from(core, "chat.send", PHONE, {}, "192.0.2.99")).kind is Kind.RESPONSE
    assert ran == ["chat.send"]
    control.observe(LAPTOP, session_report(3, False))  # over: the address means nothing any more
    assert not control.from_controlled("192.0.2.10")


async def test_a_paired_devices_message_during_a_session_counts_as_untrusted() -> None:
    from nox.ipc.handlers.core import ChatSend, CoreHandlers

    tainted: list[tuple[str, str | None]] = []
    turns: list[str] = []

    class Orchestrator:
        def conversation_for(self, channel: str) -> str:
            return f"{channel}#1"

        async def handle_text(self, text: str, **kw: Any) -> Any:
            turns.append(text)
            return SimpleNamespace(
                request_id="r", response="ok", provider="x", degraded=False, tools_used=[]
            )

    active = {"value": True}
    core = SimpleNamespace(
        orchestrator=Orchestrator(),
        security=SimpleNamespace(
            engine=SimpleNamespace(mark_tainted=lambda c, text=None: tainted.append((c, text)))
        ),
        node_chat_untrusted=lambda ctx: ctx.role == "device" and active["value"],
    )
    handlers = CoreHandlers(core)  # type: ignore[arg-type]

    async def stream(_payload: Any, _done: bool) -> None:
        return None

    phone = SimpleNamespace(role="device", client_id=PHONE, stream=stream)
    local = SimpleNamespace(role="dashboard", client_id=LOCAL, stream=stream)
    await handlers.chat_send(phone, ChatSend(text="Öffne das Terminal"))  # type: ignore[arg-type]
    await handlers.chat_send(local, ChatSend(text="Wie spät ist es?"))  # type: ignore[arg-type]
    assert tainted == [(f"chat:{PHONE}#1", None)]  # unreadable: nothing in it is user-directed
    active["value"] = False
    await handlers.chat_send(phone, ChatSend(text="Danke"))  # type: ignore[arg-type]
    assert len(tainted) == 1 and len(turns) == 3


# ---- external review of #167: wiring, the same machine, addresses in every pong -----------------


def test_the_conversation_requests_are_exactly_these() -> None:
    """C3: adding anything here (say `config.set`) must be a deliberate, reviewed change."""
    from nox.ipc.dispatch import CONVERSATION_UI_REQUESTS

    assert frozenset({"chat.send", "voice.ptt"}) == CONVERSATION_UI_REQUESTS


def test_the_installed_chat_rule_marks_paired_devices_and_the_controlled_machine() -> None:
    """T2/T3: `wire_guards` installs `node_chat_untrusted` with these answers."""
    from nox.ipc.dispatch import RequestRegistry
    from nox.node.install import wire_guards

    observed = {"host": "192.168.1.20"}
    control = NodeControl(
        None,
        lambda _n: None,
        addresses=lambda: {LAPTOP: (observed["host"], [])},
        own_addresses=lambda: ["192.168.1.5"],
    )
    core = SimpleNamespace(registry=RequestRegistry(), security=SimpleNamespace(gate=PinGate()))
    wire_guards(core, control)
    untrusted = core.node_chat_untrusted
    phone, local = request_ctx("device", PHONE), request_ctx("dashboard", LOCAL)
    assert not untrusted(phone) and not untrusted(local)  # no session
    control.answered(LAPTOP, session_report(2, True))
    assert untrusted(phone) and not untrusted(local)  # the node is another PC
    observed["host"] = "127.0.0.1"  # the node runs on this machine
    control.pull()
    assert untrusted(phone) and untrusted(local)
    observed["host"] = "192.168.1.5"  # ... connecting from one of this machine's addresses
    control.pull()
    assert control.controls_local() and untrusted(local)


async def test_on_the_controlled_machine_its_own_dashboard_cannot_approve() -> None:
    control = NodeControl(None, lambda _n: None, addresses=lambda: {LAPTOP: ("::1", [])})
    control.answered(LAPTOP, session_report(2, True))
    control.pull()

    async def ask(action: str) -> str | None:
        return await session_refusal(
            control,
            request_ctx("dashboard", LOCAL),
            "4711",
            action=action,
            gate=PinGate(),
            device_guard=None,
            remote=lambda: False,
        )

    assert await ask("security.permission.reply") == REFUSE_LINKED  # not even with the PIN
    assert await ask("plans.approve") == REFUSE_LINKED
    assert await ask("chat.send") is None  # talking works - and counts as untrusted
