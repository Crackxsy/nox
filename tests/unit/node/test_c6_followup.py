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


# ---- review: unmute coercion, remote check for talking, a browser on the controlled PC ----------


async def _send_from(core: Any, name: str, client: str, payload: dict[str, Any], peer: str) -> Any:
    from nox.ipc.dispatch import RequestContext

    role = "device" if client.startswith("device:") else "dashboard"
    envelope = _envelope(name, role, client, payload)
    ctx = RequestContext(client_id=client, role=role, request=envelope, peer=peer)
    return await core.registry.dispatch(ctx, envelope)


@pytest.mark.parametrize("muted", ["false", 0, "no", None, "", [], {}])
async def test_anything_but_a_literal_true_is_not_muting(
    monkeypatch: pytest.MonkeyPatch, muted: Any
) -> None:
    import nox.node.install as install_module
    from nox.ipc.dispatch import calm_call
    from nox.ipc.handlers.core import VoiceMute
    from nox.ipc.protocol import Kind

    monkeypatch.setattr(install_module, "console_is_remote", lambda: False)
    core, _control, _guard, _audit = _wired(Gate())
    ran: list[Any] = []

    async def handler(_ctx: Any, p: Any) -> dict[str, Any]:
        ran.append(p.muted)
        return {}

    core.registry.register("voice.mute", VoiceMute, handler, roles=("dashboard", "device"))
    assert not calm_call("voice.mute", {"muted": muted})
    refused = await _send_from(core, "voice.mute", LOCAL, {"muted": muted}, "")
    assert refused.kind is Kind.ERROR and ran == []  # the PIN rule, not the calm one
    # With the PIN the strict model still refuses a string or a number - never an "unmute".
    with_pin = await _send_from(core, "voice.mute", LOCAL, {"muted": muted, "pin": "4711"}, "")
    assert with_pin.kind is Kind.ERROR and ran == []
    assert (await _send_from(core, "voice.mute", LINKED, {"muted": True}, "")).kind is Kind.RESPONSE
    assert ran == [True]


async def test_talking_from_this_machine_keeps_the_remote_check() -> None:
    control = NodeControl(None, lambda _n: None)
    control.observe(LAPTOP, _report(2, True))

    async def ask(remote: bool | None) -> str | None:
        return await session_refusal(
            control,
            _ctx("dashboard", LOCAL),
            None,
            action="chat.send",
            gate=Gate(),
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

    core, control, ran = _registry(monkeypatch)
    control._addresses = lambda: {LAPTOP: ("192.0.2.10", ["fe80::1%eth0"])}  # type: ignore[assignment]
    control.pull()
    # An unlinked phone ... on the controlled PC's address: like the linked browser.
    for peer in ("192.0.2.10", "::ffff:192.0.2.10", "[fe80::1]"):
        reply = await _send_from(core, "chat.send", PHONE, {}, peer)
        assert reply.kind is Kind.ERROR and reply.payload["message"] == REFUSE_LINKED, peer
    assert ran == []
    # ... from another address: talking works.
    assert (await _send_from(core, "chat.send", PHONE, {}, "192.0.2.99")).kind is Kind.RESPONSE
    assert ran == ["chat.send"]
    control.observe(LAPTOP, _report(3, False))  # over: the address means nothing any more
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


def test_addresses_are_compared_in_one_spelling() -> None:
    from nox.node.agent import local_addresses
    from nox.node.world_routes import host_of

    assert host_of("[FE80::1%eth0]") == "fe80::1"
    assert host_of("::ffff:192.0.2.10") == "192.0.2.10"
    assert host_of("") == ""
    assert all("%" not in a for a in local_addresses())


def _addressed(reported: list[str]) -> NodeControl:
    control = NodeControl(
        None, lambda _n: None, addresses=lambda: {LAPTOP: ("192.168.1.20", reported)}
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    return control


def test_the_nodes_own_connection_address_is_the_primary_signal() -> None:
    control = _addressed([])
    assert control.from_controlled("192.168.1.20")


def test_a_node_cannot_claim_a_public_address() -> None:
    control = _addressed(["198.51.100.7", "2001:db8::7", "192.168.1.21", "fd00::21"])
    assert not control.from_controlled("198.51.100.7")  # public: ignored
    assert not control.from_controlled("2001:db8::7")
    assert control.from_controlled("192.168.1.21") and control.from_controlled("fd00::21")


def test_a_matching_address_is_refused_even_if_another_device_uses_it() -> None:
    """Fail closed (owner decision): a device sharing the controlled PC's address is treated as
    that PC. The possible lock-out of another device is accepted and documented."""
    control = _addressed(["192.168.1.50"])
    assert control.from_controlled("192.168.1.50")


def test_a_disconnected_but_locked_node_keeps_its_addresses_until_the_lock_lifts() -> None:
    connected = {"value": True}
    control = NodeControl(
        None,
        lambda _n: None,
        addresses=lambda: {LAPTOP: ("192.168.1.20", [])} if connected["value"] else {},
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    connected["value"] = False  # gone, but the lock holds (it never said the session ended)
    control.pull()
    assert control.from_controlled("192.168.1.20")
    control.observe(LAPTOP, _report(3, False))  # the lock lifts
    control.pull()
    assert control.node_addresses == {} and not control.from_controlled("192.168.1.20")


def test_at_most_sixteen_reported_addresses_count() -> None:
    reported = [f"10.0.0.{i}" for i in range(1, 30)]
    control = _addressed(reported)
    assert control.from_controlled("10.0.0.16") and not control.from_controlled("10.0.0.17")


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
    core = SimpleNamespace(registry=RequestRegistry(), security=SimpleNamespace(gate=Gate()))
    wire_guards(core, control)
    untrusted = core.node_chat_untrusted
    phone, local = _ctx("device", PHONE), _ctx("dashboard", LOCAL)
    assert not untrusted(phone) and not untrusted(local)  # no session
    control.observe(LAPTOP, _report(2, True))
    assert untrusted(phone) and not untrusted(local)  # the node is another PC
    observed["host"] = "127.0.0.1"  # the node runs on this machine
    control.pull()
    assert untrusted(phone) and untrusted(local)
    observed["host"] = "192.168.1.5"  # ... connecting from one of this machine's addresses
    control.pull()
    assert control.controls_local() and untrusted(local)


async def test_on_the_controlled_machine_its_own_dashboard_cannot_approve() -> None:
    control = NodeControl(None, lambda _n: None, addresses=lambda: {LAPTOP: ("::1", [])})
    control.observe(LAPTOP, _report(2, True))
    control.pull()

    async def ask(action: str) -> str | None:
        return await session_refusal(
            control,
            _ctx("dashboard", LOCAL),
            "4711",
            action=action,
            gate=Gate(),
            device_guard=None,
            remote=lambda: False,
        )

    assert await ask("security.permission.reply") == REFUSE_LINKED  # not even with the PIN
    assert await ask("plans.approve") == REFUSE_LINKED
    assert await ask("chat.send") is None  # talking works - and counts as untrusted


async def test_the_node_says_where_it_is_in_its_hello_and_every_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: `NodeHello.addresses` and `nox/pong` `addresses`, reused for a while."""
    from nox.node import agent as agent_module
    from nox.node.protocol import NodeHello, Ping

    calls: list[int] = []

    def addresses() -> list[str]:
        calls.append(1)
        return ["192.168.1.20", "fd00::20"]

    monkeypatch.setattr(agent_module, "local_addresses", addresses)
    agent = object.__new__(agent_module.NodeAgent)
    first = agent._addresses_now()
    again = agent._addresses_now()
    assert first == again == ["192.168.1.20", "fd00::20"] and len(calls) == 1  # cached
    hello = {
        "protocolVersion": "1",
        "node_protocol": 1,
        "node_version": "1",
        "os": "windows",
        "arch": "AMD64",
        "manifest_hash": "0" * 64,
        "addresses": first,
    }
    assert NodeHello.model_validate(hello).addresses == first
    pong = Ping.model_validate({"seq": 1, "ts": 1.0, "addresses": first})
    assert pong.addresses == first


def test_real_interfaces_come_before_virtual_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from nox.node.agent import local_addresses

    def entry(address: str) -> Any:
        return SimpleNamespace(address=address)

    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "vEthernet (WSL)": [entry("172.20.0.1")],
            "Ethernet": [entry("192.168.1.20"), entry("fe80::20%12")],
            "Loopback": [entry("127.0.0.1")],
            "Down": [entry("192.168.9.9")],
            "Public": [entry("198.51.100.7")],
        },
    )
    monkeypatch.setattr(
        psutil,
        "net_if_stats",
        lambda: {
            "vEthernet (WSL)": SimpleNamespace(isup=True),
            "Ethernet": SimpleNamespace(isup=True),
            "Down": SimpleNamespace(isup=False),
            "Public": SimpleNamespace(isup=True),
        },
    )
    found = local_addresses()
    assert found[:2] == ["192.168.1.20", "fe80::20"]
    assert found.index("172.20.0.1") > 1 and "127.0.0.1" not in found
    assert found.index("192.168.9.9") > found.index("172.20.0.1")


# ---- commit review: the same-machine list is complete, the binding never shrinks ---------------


def test_this_machine_is_recognised_whatever_its_number_of_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.node.agent import all_local_addresses

    adapters = {f"vEthernet ({i})": [SimpleNamespace(address=f"172.20.{i}.1")] for i in range(30)}
    adapters["Ethernet"] = [
        SimpleNamespace(address="192.168.1.5"),
        SimpleNamespace(address="00-11-22-33-44-55"),
    ]
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: adapters)
    everything = all_local_addresses()
    assert len(everything) == 32 and "192.168.1.5" in everything  # uncapped, unfiltered
    control = NodeControl(
        None,
        lambda _n: None,
        addresses=lambda: {LAPTOP: ("192.168.1.5", [])},
        own_addresses=all_local_addresses,
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    assert control.controls_local()


def test_an_address_that_cannot_be_classified_counts_as_this_machine() -> None:
    control = NodeControl(
        None, lambda _n: None, addresses=lambda: {LAPTOP: ("pc.example.invalid", [])}
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    assert control.controls_local()


def test_the_binding_only_grows_while_the_lock_holds() -> None:
    now: dict[str, Any] = {"peer": "192.168.1.20", "reported": ["192.168.1.21", "fd00::21"]}
    control = NodeControl(
        None, lambda _n: None, addresses=lambda: {LAPTOP: (now["peer"], now["reported"])}
    )
    control.observe(LAPTOP, _report(2, True))
    control.pull()
    now.update(peer="192.168.1.30", reported=[])  # a later pong says less, and it reconnected
    control.pull()
    for host in ("192.168.1.20", "192.168.1.21", "fd00::21", "192.168.1.30"):
        assert control.from_controlled(host), host
    assert control.node_observed[LAPTOP] == {"192.168.1.20", "192.168.1.30"}
    control.observe(LAPTOP, _report(3, False))  # the lock lifts: everything goes
    control.pull()
    assert control.node_addresses == {} and control.node_observed == {}
