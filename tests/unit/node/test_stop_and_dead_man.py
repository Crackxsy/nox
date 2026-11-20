"""Stopping a node: a kill from the core stops running calls and holds until a resume, a
stop on the device holds until the device resumes, the dead-man rule aborts a side effect
when the core goes silent and lifts when it is back, and an expired certificate ends the
session. The node is driven by the real listener over real TLS."""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.device import agent as agent_module
from nox.node.device.agent import NodeAgent
from nox.node.device.gate import NodeGate
from nox.node.device.home import NodeHome
from nox.node.device.keystore import NodeKeyStore
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import NodeManifest
from nox.node.wire.receipts import ReceiptSigner
from tests.unit.node.support import (
    Confirmer,
    call,
    kill_switch_for,
    percentile,
    scripted_tools,
    session_of,
    start_node,
)

# ---- stopping ----------------------------------------------------------------------------------


async def test_a_kill_stops_running_calls_and_holds_until_resume(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        running = asyncio.create_task(call(session, listener.signer, "system.wait", timeout=30))  # type: ignore[attr-defined]
        for _ in range(100):
            if node.ran:
                break
            await asyncio.sleep(0.02)
        started = time.monotonic()
        acked = await listener.broadcast_kill("test")
        assert acked == {node.node_id: True}
        assert time.monotonic() - started < 1.0
        result = await asyncio.wait_for(running, 2)
        assert result["_nox"]["refused"] == "stopped"
        assert "wait-end" not in node.ran  # nothing completes after nox/killed
        assert session.killed_calls  # the node named the call it stopped
        refused = await call(session, listener.signer, "system.low")  # type: ignore[attr-defined]
        assert refused["_nox"]["refused"] == "stopped_by_core"
        await listener.broadcast_resume()
        for _ in range(100):
            if node.agent.stopped_reason() is None:
                break
            await asyncio.sleep(0.02)
        ok = await call(session, listener.signer, "system.low")  # type: ignore[attr-defined]
        assert "_nox" not in ok and "low" in node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_a_stop_on_the_device_stops_everything_and_only_the_device_resumes(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        node.home.stop_file.write_text("stop")
        for _ in range(100):
            if listener.kills:  # type: ignore[attr-defined]
                break
            await asyncio.sleep(0.02)
        assert listener.kills and listener.kills[0][0] == node.node_id  # type: ignore[attr-defined]
        refused = await call(session, listener.signer, "system.low")  # type: ignore[attr-defined]
        assert refused["_nox"]["refused"] == "stopped_on_device"
        await listener.broadcast_resume()  # the core cannot lift a stop made on the device
        await asyncio.sleep(0.1)
        again = await call(session, listener.signer, "system.low")  # type: ignore[attr-defined]
        assert again["_nox"]["refused"] == "stopped_on_device"
        node.home.stop_file.unlink()
        for _ in range(100):
            if node.agent.stopped_reason() is None:
                break
            await asyncio.sleep(0.02)
        assert "_nox" not in await call(session, listener.signer, "system.low")  # type: ignore[attr-defined]
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_the_dead_man_aborts_a_side_effect_when_the_core_goes_silent(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "DEAD_MAN_S", 0.5)
    monkeypatch.setattr(agent_module, "DEAD_MAN_TICK_S", 0.05)
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        started = time.monotonic()
        # The listener fixture pings once an hour: after the call, the core says nothing.
        result = await call(session, listener.signer, "system.wait", timeout=10)  # type: ignore[attr-defined]
        assert result["_nox"]["refused"] == "dead_man"
        assert time.monotonic() - started < 3.0 and "wait-end" not in node.ran
        assert node.agent.stopped_reason() == "dead_man"
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_an_expired_certificate_ends_the_session(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """A session does not outlive its certificate."""
    from nox.node.hub.repo import NodeRepository

    signer = ReceiptSigner(tmp_path / "tls")
    server = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(pairing._repo._db),
        tls=lambda: tls,  # type: ignore[attr-defined]
        host="127.0.0.1",
        port=0,
        receipt_key=signer.public_key,
        ping_interval_s=0.05,
    )
    server.signer = signer  # type: ignore[attr-defined]
    await server.start()
    node = await start_node(server, pairing, tls, tmp_path)
    try:
        session = await session_of(server, node.node_id)
        session.cert_not_after = datetime.now(UTC) - timedelta(seconds=1)
        for _ in range(100):
            if node.node_id not in server.sessions or server.sessions[node.node_id] is not session:
                break
            await asyncio.sleep(0.02)
        assert server.sessions.get(node.node_id) is not session
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)
        await server.stop()


async def test_the_stop_reaches_five_loopback_nodes_quickly(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Stopping, as far as a single test runner can show it: five nodes on loopback in one
    process, a long call on each, the kill switch engaged -> every `nox/killed`. Each round is
    timed from `KillSwitchService.engage()` until the last node's `nox/killed` arrived. 20
    rounds; the p99 (and so the slowest round) stays under a second and no call completes after
    its node said killed. With separate node processes and a real sandboxed `sleep 60`:
    `tests/integration/test_node_kill_processes.py`; over Wi-Fi on a Pi and PCs it belongs to
    the hardware checklist."""
    switch, all_killed, acks = kill_switch_for(listener)
    nodes = [await start_node(listener, pairing, tls, tmp_path) for _ in range(5)]
    try:
        sessions = [await session_of(listener, n.node_id) for n in nodes]
        durations: list[float] = []
        for _round in range(20):
            running = [
                asyncio.create_task(call(s, listener.signer, "system.wait", timeout=30))  # type: ignore[attr-defined]
                for s in sessions
            ]
            for _ in range(200):
                if all(n.ran.count("wait-start") > _round for n in nodes):
                    break
                await asyncio.sleep(0.01)
            started = time.monotonic()
            await switch.engage("ui", "latency test")
            durations.append(all_killed[-1] - started)
            assert len(acks[-1]) == 5 and all(acks[-1].values()), acks[-1]
            assert all(s.killed.is_set() for s in sessions)  # every `nox/killed` is in
            results = await asyncio.gather(*running)
            assert all(r["_nox"]["refused"] == "stopped" for r in results)
            assert await switch.resume(pin_ok=False, by="test")
            for _ in range(200):
                if all(n.agent.stopped_reason() is None for n in nodes):
                    break
                await asyncio.sleep(0.01)
        assert not any("wait-end" in n.ran for n in nodes)
        p99 = percentile(durations, 99)
        assert p99 < 1.0, (p99, statistics.mean(durations))
        assert max(durations) < 1.0, (max(durations), statistics.mean(durations))
    finally:
        for n in nodes:
            n.stop.set()
        await asyncio.gather(*(asyncio.wait_for(n.task, 5) for n in nodes))


# ---- the core's side ---------------------------------------------------------------------------


async def test_the_kill_switch_resumes_the_nodes() -> None:
    from nox.security.killswitch import KillSwitchService

    resumed: list[str] = []

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    switch = KillSwitchService(audit=Audit())  # type: ignore[arg-type]

    async def on_resume() -> None:
        resumed.append("nodes")

    switch.register_resume_hook("nodes", on_resume)
    await switch.engage("ui", "test")
    assert await switch.resume(pin_ok=False, by="dashboard")
    assert resumed == ["nodes"]


# ---- a stop that arrives at an awkward moment ----------------------------------------------------


async def test_a_node_that_connects_while_nox_is_stopped_stays_stopped(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    from nox.node.hub.repo import NodeRepository

    signer = ReceiptSigner(tmp_path / "tls")
    server = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(pairing._repo._db),
        tls=lambda: tls,  # type: ignore[attr-defined]
        host="127.0.0.1",
        port=0,
        receipt_key=signer.public_key,
        ping_interval_s=3600,
        kill_engaged=lambda: True,
    )
    server.signer = signer  # type: ignore[attr-defined]
    await server.start()
    node = await start_node(server, pairing, tls, tmp_path)
    try:
        session = await session_of(server, node.node_id)
        refused = await call(session, signer, "system.low")
        assert refused["_nox"]["refused"] == "stopped_by_core"
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)
        await server.stop()


async def test_a_stop_closes_an_open_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node.device import confirm as confirm_module

    killed: list[bool] = []

    class Proc:
        async def wait(self) -> int:
            await asyncio.sleep(3600)
            return 0

        def kill(self) -> None:
            killed.append(True)

    async def fake_exec(*_argv: str, **_kw: Any) -> Proc:
        return Proc()

    monkeypatch.setattr(confirm_module.sys, "platform", "win32")
    monkeypatch.setattr(confirm_module.asyncio, "create_subprocess_exec", fake_exec)
    asking = asyncio.create_task(confirm_module.PlatformConfirmer().ask("x"))
    await asyncio.sleep(0.05)
    asking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asking
    assert killed == [True]


def test_a_stop_on_the_device_holds_even_before_the_node_noticed(tmp_path: Path) -> None:
    """The stop file decides at once - also while the node is not connected or between polls."""
    home = NodeHome(tmp_path / "h")
    home.ensure()
    gate = NodeGate(scripted_tools([]), NodeManifest(), None)
    agent = NodeAgent(home, NodeKeyStore(home.key_file, None), gate, None, confirmer=Confirmer())
    assert agent.stopped_reason() is None
    home.stop_file.write_text("stop")
    assert agent.stopped_reason() == "stopped_on_device"
    assert gate.check("system.info", {}).reason == "stopped_on_device"


# ---- a dead-man stop lifts when the core is back; the core sees the state ------------------------


async def test_the_dead_man_stop_lifts_after_pings_and_the_core_sees_it(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        node.agent.dead_man = True
        for seq in range(1, agent_module.DEAD_MAN_RESET_PINGS + 1):
            await session.client.notify("nox/ping", {"seq": seq, "ts": time.time()})
            for _ in range(100):
                if session.node_state != "" or node.agent.dead_man is False:
                    break
                await asyncio.sleep(0.01)
            if seq == 1:
                assert session.node_state == "dead_man"  # reported to the core
        for _ in range(100):
            if not node.agent.dead_man:
                break
            await asyncio.sleep(0.01)
        assert node.agent.dead_man is False and node.agent.stopped_reason() is None
        # While the core itself is stopped, pings do not lift it.
        node.agent.dead_man, node.agent.core_killed = True, True
        for seq in range(10, 14):
            await session.client.notify("nox/ping", {"seq": seq, "ts": time.time()})
        await asyncio.sleep(0.2)
        assert node.agent.dead_man is True
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)
