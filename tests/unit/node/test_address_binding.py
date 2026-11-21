"""Which connections count as the controlled PC: the node's own connection address first,
then what it reports, compared in one spelling. The binding only grows while the lock
holds, and when it is full everyone is treated as the controlled PC."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.world_routes import NodeActions, NodeControl, NodeLookups
from nox.node.wire import protocol as p
from tests.unit.fakes import wait_until
from tests.unit.node.support import session_of, start_node
from tests.unit.node.support_sessions import (
    LAPTOP,
    LOCAL,
    PHONE,
    Text,
    conversation_registry,
    request_ctx,
    send_from,
    session_report,
)


def test_addresses_are_compared_in_one_spelling() -> None:
    from nox.node.device.agent.netinfo import local_addresses
    from nox.node.hub.world_routes import host_of

    assert host_of("[FE80::1%eth0]") == "fe80::1"
    assert host_of("::ffff:192.0.2.10") == "192.0.2.10"
    assert host_of("") == ""
    assert all("%" not in a for a in local_addresses())


def _addressed(reported: list[str]) -> NodeControl:
    control = NodeControl(
        None, lambda _n: None, NodeLookups(addresses=lambda: {LAPTOP: ("192.168.1.20", reported)})
    )
    control.answered(LAPTOP, session_report(2, True))
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
    """Fail closed: a device sharing the controlled PC's address is treated as
    that PC. The possible lock-out of another device is accepted and documented."""
    control = _addressed(["192.168.1.50"])
    assert control.from_controlled("192.168.1.50")


def test_a_disconnected_but_locked_node_keeps_its_addresses_until_the_lock_lifts() -> None:
    connected = {"value": True}
    control = NodeControl(
        None,
        lambda _n: None,
        NodeLookups(addresses=lambda: {LAPTOP: ("192.168.1.20", [])} if connected["value"] else {}),
    )
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    connected["value"] = False  # gone, but the lock holds (it never said the session ended)
    control.pull()
    assert control.from_controlled("192.168.1.20")
    control.observe(LAPTOP, session_report(3, False))  # the lock lifts
    control.pull()
    assert control.node_addresses == {} and not control.from_controlled("192.168.1.20")


def test_at_most_sixteen_reported_addresses_count() -> None:
    reported = [f"10.0.0.{i}" for i in range(1, 30)]
    control = _addressed(reported)
    assert control.from_controlled("10.0.0.16") and not control.from_controlled("10.0.0.17")


async def test_the_node_says_where_it_is_in_its_hello_and_every_pong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NodeHello.addresses` and `nox/pong` `addresses`, reused for a while."""
    from nox.node.device.agent import node_agent as agent_module
    from nox.node.wire.protocol import NodeHello, Ping

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

    from nox.node.device.agent.netinfo import local_addresses

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


# ---- the same-machine list is complete, the binding never shrinks --------------------------------


def test_this_machine_is_recognised_whatever_its_number_of_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.node.wire.addresses import all_local_addresses

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
        NodeLookups(
            addresses=lambda: {LAPTOP: ("192.168.1.5", [])}, own_addresses=all_local_addresses
        ),
    )
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    assert control.controls_local()


def test_an_address_that_cannot_be_classified_counts_as_this_machine() -> None:
    control = NodeControl(
        None, lambda _n: None, NodeLookups(addresses=lambda: {LAPTOP: ("pc.example.invalid", [])})
    )
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    assert control.controls_local()


def test_the_binding_only_grows_while_the_lock_holds() -> None:
    now: dict[str, Any] = {"peer": "192.168.1.20", "reported": ["192.168.1.21", "fd00::21"]}
    control = NodeControl(
        None,
        lambda _n: None,
        NodeLookups(addresses=lambda: {LAPTOP: (now["peer"], now["reported"])}),
    )
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    now.update(peer="192.168.1.30", reported=[])  # a later pong says less, and it reconnected
    control.pull()
    for host in ("192.168.1.20", "192.168.1.21", "fd00::21", "192.168.1.30"):
        assert control.from_controlled(host), host
    assert control.node_observed[LAPTOP] == {"192.168.1.20", "192.168.1.30"}
    control.observe(LAPTOP, session_report(3, False))  # the lock lifts: everything goes
    control.pull()
    assert control.node_addresses == {} and control.node_observed == {}


def _saturating(control: NodeControl) -> dict[str, Any]:
    now: dict[str, Any] = {"peer": "192.168.1.20", "reported": []}
    control._addresses = lambda: {LAPTOP: (now["peer"], now["reported"])}  # type: ignore[assignment]
    return now


def test_a_full_binding_drops_nothing_and_treats_everyone_as_the_controlled_pc() -> None:
    from nox.node.hub.world_routes import MAX_REPORTED_BOUND

    audited: list[str] = []
    control = NodeControl(None, lambda _n: None, actions=NodeActions(audit=audited.append))
    now = _saturating(control)
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    first = [f"10.0.0.{i}" for i in range(16)]
    now["reported"] = first
    control.pull()
    for block in range(1, 10):  # a node that keeps reporting new addresses
        now["reported"] = [f"10.{block}.0.{i}" for i in range(16)]
        control.pull()
    assert len(control.node_reported[LAPTOP]) == MAX_REPORTED_BOUND
    assert control.node_reported[LAPTOP][:16] == first  # nothing evicted, the first ones kept
    assert control.from_controlled("192.168.1.20")  # the first observed address stays
    assert control.is_saturated() and len(audited) == 1
    # Everyone counts as the controlled PC now.
    assert control.from_controlled("198.51.100.99") and control.controls_local()
    control.observe(LAPTOP, session_report(3, False))  # the lock lifts: so does the saturation
    control.pull()
    assert not control.is_saturated() and control.saturated == set()


def test_too_many_connection_addresses_saturate_as_well() -> None:
    from nox.node.hub.world_routes import MAX_OBSERVED_ADDRESSES

    control = NodeControl(None, lambda _n: None)
    now = _saturating(control)
    control.answered(LAPTOP, session_report(2, True))
    for i in range(MAX_OBSERVED_ADDRESSES + 1):
        now["peer"] = f"192.168.2.{i}"
        control.pull()
    assert len(control.node_observed[LAPTOP]) == MAX_OBSERVED_ADDRESSES
    assert "192.168.2.0" in control.node_observed[LAPTOP] and control.is_saturated()


async def test_while_saturated_approvals_and_messages_wait_the_rest_follows_the_pin() -> None:
    from nox.ipc.handlers.core import CoreHandlers, PermissionReply
    from nox.ipc.protocol import Kind
    from nox.security.access import REFUSE_SATURATED
    from nox.security.model import Decision

    core, control, ran = conversation_registry()
    roles = ("dashboard", "shell", "device")

    async def kill(_ctx: Any, _p: Any) -> dict[str, Any]:
        ran.append("security.kill")
        return {}

    core.registry.register("security.kill", Text, kill, roles=roles)
    control.saturated.add(LAPTOP)
    for client in (LOCAL, PHONE):
        reply = await send_from(core, "chat.send", client, {}, "192.0.2.99")
        assert reply.kind is Kind.ERROR and reply.payload["message"] == REFUSE_SATURATED.reason
        assert reply.payload["details"]["sub_reason"] == "node_session_saturated"
        # Saturation binds approvals and messages only: the rest follows the PIN.
        approve = await send_from(core, "plans.approve", client, {"pin": "4711"}, "192.0.2.99")
        assert approve.kind is Kind.RESPONSE
        assert (await send_from(core, "security.kill", client, {}, "")).kind is Kind.RESPONSE
    assert ran == ["plans.approve", "security.kill"] * 2
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    local = request_ctx("dashboard", LOCAL)
    allow = PermissionReply(grant_id="g", decision=Decision.ALLOW, pin="4711")
    deny = PermissionReply(grant_id="g", decision=Decision.DENY)
    assert (await handlers.permission_reply(local, allow))["ok"] is False
    assert (await handlers.permission_reply(local, deny))["ok"] is True


# ---- the addresses a node reports, end to end ---------------------------------------


async def test_the_listener_takes_the_addresses_the_agent_sends_in_hello_and_pongs(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.node.device.agent import node_agent as agent_module

    current = {"addresses": ["192.0.2.10", "fe80::10"]}
    monkeypatch.setattr(agent_module, "local_addresses", lambda: list(current["addresses"]))
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        assert session.reported_addresses == ["192.0.2.10", "fe80::10"]  # from the hello
        current["addresses"] = ["192.0.2.11"]
        node.agent._addresses_cache = None  # type: ignore[attr-defined]  # the TTL ran out
        await session.client.notify(p.M_PING, {"seq": 1, "ts": time.time()})
        await wait_until(
            lambda: session.reported_addresses == ["192.0.2.11"],
            what="the listener never took the address the node reported",
        )
        assert session.peer_host  # the connection's own address is known as well
    finally:
        node.stop.set()
        await node.task


# ---- this machine's own addresses ------------------------------------------------------


def test_unreadable_own_addresses_count_as_this_machine_during_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.node.hub.world_routes import NodeControl, NodeLookups
    from nox.node.wire.addresses import ADDRESSES_UNREADABLE, all_local_addresses

    def broken() -> Any:
        raise PermissionError("no access to the interface table")

    monkeypatch.setattr(psutil, "net_if_addrs", broken)
    assert all_local_addresses() == [ADDRESSES_UNREADABLE]  # never an empty list
    control = NodeControl(
        None,
        lambda _n: None,
        NodeLookups(
            addresses=lambda: {LAPTOP: ("192.0.2.20", [])}, own_addresses=all_local_addresses
        ),
    )
    assert not control.controls_local()  # no session: nothing to protect
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    assert control.controls_local()  # cannot tell this machine apart: it is the controlled PC


def test_an_own_address_lookup_that_raises_fails_closed() -> None:
    from nox.node.hub.world_routes import NodeControl, NodeLookups

    def raising() -> list[str]:
        raise RuntimeError("psutil is gone")

    control = NodeControl(
        None,
        lambda _n: None,
        NodeLookups(addresses=lambda: {LAPTOP: ("192.0.2.20", [])}, own_addresses=raising),
    )
    control.answered(LAPTOP, session_report(2, True))
    control.pull()
    assert control.controls_local()
