"""C4: the node's own gate, the confirmation on the device, and stopping everywhere in under a
second. Every control has a test that goes red without it; the node is driven by the real
listener over real TLS, the core side by a test that sends exactly what it wants."""

from __future__ import annotations

import asyncio
import itertools
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.node import agent as agent_module
from nox.node.agent import NodeAgent, confirmation_text
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.listener import NodeListener, NodeSession
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier
from nox.node.tools import NodeTool, default_tools
from nox.security.audit import SqliteAuditLog
from tests.unit.node.conftest import key_pem, pair_node

_IDS = itertools.count()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "x"


class Confirmer:
    def __init__(self, answer: str = "allow", *, available: bool = True) -> None:
        self.answer, self._available, self.asked = answer, available, []  # type: ignore[var-annotated]

    @property
    def available(self) -> bool:
        return self._available

    async def ask(self, text: str, *, timeout_s: int = 60) -> str:
        self.asked.append(text)
        return self.answer


@dataclass
class Node:
    agent: NodeAgent
    home: NodeHome
    node_id: str
    task: asyncio.Task[str]
    stop: asyncio.Event
    ran: list[str]
    audit: SqliteAuditLog


def _tools(ran: list[str]) -> dict[str, NodeTool]:
    async def wait(_a: BaseModel) -> dict[str, Any]:
        ran.append("wait-start")
        await asyncio.sleep(60)
        ran.append("wait-end")
        return {}

    async def high(_a: BaseModel) -> dict[str, Any]:
        ran.append("high")
        return {"done": True}

    async def low(_a: BaseModel) -> dict[str, Any]:
        ran.append("low")
        return {"done": True}

    tools = default_tools(None)
    tools["system.wait"] = NodeTool("system.wait", "system", "low", "t", _Args, wait)
    tools["system.high"] = NodeTool("system.high", "system", "high", "t", _Args, high)
    tools["system.low"] = NodeTool("system.low", "system", "low", "t", _Args, low)
    return tools


async def start_node(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    *,
    confirmer: Confirmer | None = None,
    manifest: NodeManifest | None = None,
    receipt_key: str = "",
) -> Node:
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(tmp_path / f"home-{node.node_id[:6]}")
    home.ensure()
    home.key_file.write_bytes(key_pem(node.key))
    home.save_pairing(
        NodeIdentity(
            node_id=node.node_id,
            name="Laptop",
            core_host="127.0.0.1",
            core_port=47843,
            node_port=listener.port,
            ca_fingerprint=tls.fingerprint.replace(":", "").lower(),
            paired_at=datetime.now(UTC),
            receipt_key=receipt_key,
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    ran: list[str] = []
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    gate = NodeGate(_tools(ran), manifest or NodeManifest(), audit, protected=home.path)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        gate,
        audit,
        ping_interval_s=3600,
        confirmer=confirmer or Confirmer(),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    return Node(agent, home, node.node_id, task, stop, ran, audit)


async def session_of(listener: NodeListener, node_id: str) -> NodeSession:
    for _ in range(300):
        session = listener.sessions.get(node_id)
        if session is not None and session.ready.is_set():
            return session
        await asyncio.sleep(0.02)
    raise AssertionError("the node never connected")


def signed(
    signer: ReceiptSigner,
    name: str,
    arguments: Any,
    *,
    risk: str = "low",
    confirm_on_device: bool = False,
    deadline: float | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    call_id = call_id or f"k{next(_IDS)}"
    deadline = time.time() + 30 if deadline is None else deadline
    return {
        "call_id": call_id,
        "risk": risk,
        "deadline": deadline,
        "decided_by": "core",
        "confirm_on_device": confirm_on_device,
        "receipt": signer.sign(
            call_id=call_id,
            tool=name,
            arguments=arguments,
            risk=risk,
            decided_by="core",
            confirm_on_device=confirm_on_device,
            deadline=deadline,
        ),
    }


async def call(
    session: NodeSession,
    signer: ReceiptSigner,
    name: str,
    arguments: Any | None = None,
    *,
    timeout: float = 10,
    **meta: Any,
) -> dict[str, Any]:
    args = {} if arguments is None else arguments
    params = {"name": name, "arguments": args, "_nox": signed(signer, name, args, **meta)}
    return await session.client.request("tools/call", params, timeout_s=timeout)


# ---- receipts ----------------------------------------------------------------------------------


def _verifier_and_signer(tmp_path: Path) -> tuple[ReceiptVerifier, ReceiptSigner]:
    signer = ReceiptSigner(tmp_path / "r")
    return ReceiptVerifier(signer.public_key), signer


def _check(
    verifier: ReceiptVerifier,
    meta: dict[str, Any],
    *,
    tool: str = "system.low",
    arguments: Any = None,
    **over: Any,
) -> str | None:
    fields = {
        "receipt": meta["receipt"],
        "call_id": meta["call_id"],
        "tool": tool,
        "arguments": {} if arguments is None else arguments,
        "risk": meta["risk"],
        "decided_by": meta["decided_by"],
        "confirm_on_device": meta["confirm_on_device"],
        "deadline": meta["deadline"],
    }
    fields.update(over)
    return verifier.check(**fields)


def test_a_receipt_binds_exactly_this_call(tmp_path: Path) -> None:
    verifier, signer = _verifier_and_signer(tmp_path)
    meta = signed(signer, "system.low", {"target": "a"})
    assert _check(verifier, meta, arguments={"target": "b"}) == "receipt_invalid"
    assert (
        _check(verifier, meta, arguments={"target": "a"}, tool="system.high") == "receipt_invalid"
    )
    assert _check(verifier, meta, arguments={"target": "a"}, risk="read") == "receipt_invalid"
    assert _check(verifier, meta, arguments={"target": "a"}, confirm_on_device=True) == (
        "receipt_invalid"
    )
    assert _check(verifier, meta, arguments={"target": "a"}) is None
    assert _check(verifier, meta, arguments={"target": "a"}) == "receipt_replayed"


def test_receipts_expire_and_cannot_be_made_long_lived(tmp_path: Path) -> None:
    verifier, signer = _verifier_and_signer(tmp_path)
    old = signed(signer, "system.low", {}, deadline=time.time() - 30)
    assert _check(verifier, old) == "receipt_expired"
    long = signed(signer, "system.low", {}, deadline=time.time() + 600)
    assert _check(verifier, long) == "receipt_too_long"
    assert _check(verifier, {**old, "receipt": ""}) == "receipt_missing"


def test_another_key_signs_nothing(tmp_path: Path) -> None:
    verifier, _signer = _verifier_and_signer(tmp_path)
    other = ReceiptSigner(tmp_path / "other")
    assert _check(verifier, signed(other, "system.low", {})) == "receipt_invalid"


def test_clock_skew_is_measured_from_the_cores_pings(tmp_path: Path) -> None:
    signer = ReceiptSigner(tmp_path / "r")
    core_ahead = 300.0
    verifier = ReceiptVerifier(signer.public_key, clock=lambda: time.time() - core_ahead)
    meta = signed(signer, "system.low", {})
    assert _check(verifier, meta) == "receipt_too_long"  # the node's clock is 5 min behind
    verifier.observe_core_time(time.time())
    assert _check(verifier, signed(signer, "system.low", {})) is None


# ---- the gate's own limits ---------------------------------------------------------------------


def _gate(manifest: NodeManifest | None = None, *, signer: ReceiptSigner) -> NodeGate:
    gate = NodeGate(_tools([]), manifest or NodeManifest(), None)
    gate.receipts = ReceiptVerifier(signer.public_key)
    return gate


def _meta(signer: ReceiptSigner, name: str, args: Any, **kw: Any) -> NoxCallMeta:
    return NoxCallMeta.model_validate(signed(signer, name, args, **kw))


def test_the_core_may_not_understate_the_risk(tmp_path: Path) -> None:
    signer = ReceiptSigner(tmp_path / "r")
    gate = _gate(signer=signer)
    meta = _meta(signer, "system.high", {}, risk="low")
    assert gate.check("system.high", {}, meta=meta).reason == "risk_understated"


def test_the_node_keeps_its_own_budget(tmp_path: Path) -> None:
    signer = ReceiptSigner(tmp_path / "r")
    gate = _gate(NodeManifest(max_calls_per_hour=2, max_high_per_hour=1), signer=signer)
    for _ in range(2):
        assert gate.check("system.low", {}, meta=_meta(signer, "system.low", {})).allowed
    assert gate.check("system.low", {}, meta=_meta(signer, "system.low", {})).reason == "budget"
    gate2 = _gate(NodeManifest(max_high_per_hour=1), signer=signer)
    high = _meta(signer, "system.high", {}, risk="high")
    assert gate2.check("system.high", {}, meta=high).allowed
    again = _meta(signer, "system.high", {}, risk="high")
    assert gate2.check("system.high", {}, meta=again).reason == "budget"


def test_max_risk_lowers_the_ceiling_and_reads_need_no_receipt(tmp_path: Path) -> None:
    signer = ReceiptSigner(tmp_path / "r")
    gate = _gate(NodeManifest(max_risk="medium"), signer=signer)
    meta = _meta(signer, "system.high", {}, risk="high")
    assert gate.check("system.high", {}, meta=meta).reason == "risk_ceiling"
    assert gate.check("system.info", {}).allowed  # read: no receipt needed


def test_a_side_effect_without_a_receipt_key_is_refused(tmp_path: Path) -> None:
    gate = NodeGate(_tools([]), NodeManifest(), None)
    signer = ReceiptSigner(tmp_path / "r")
    assert gate.check("system.low", {}, meta=_meta(signer, "system.low", {})).reason == (
        "receipt_missing"
    )


@pytest.mark.parametrize(
    ("tool_risk", "meta_risk", "delegated", "available", "answer", "trusted"),
    list(
        itertools.product(
            ("low", "high"),
            ("low", "high"),
            (False, True),
            (False, True),
            ("allow", "deny", "timeout"),
            (False, True),
        )
    ),
)
async def test_nothing_high_ever_runs_without_a_confirmation(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    tool_risk: str,
    meta_risk: str,
    delegated: bool,
    available: bool,
    answer: str,
    trusted: bool,
) -> None:
    """The matrix of the review's HIGH finding: core risk x node risk x delegation x a confirmer
    on the device x its answer x `trust_core_for_high`."""
    if _rank(meta_risk) < _rank(tool_risk):
        pytest.skip("understating the risk is refused before anything else (tested above)")
    confirmer = Confirmer(answer, available=available)
    node = await start_node(
        listener,
        pairing,
        tls,
        tmp_path,
        confirmer=confirmer,
        manifest=NodeManifest(trust_core_for_high=trusted),
    )
    try:
        session = await session_of(listener, node.node_id)
        name = "system.high" if tool_risk == "high" else "system.low"
        result = await call(
            session,
            listener.signer,
            name,
            {"target": "Arbeits-PC"},  # type: ignore[attr-defined]
            risk=meta_risk,
            confirm_on_device=delegated,
        )
        ran = bool(node.ran)
        needs = tool_risk == "high" or meta_risk == "high" or delegated
        if not needs:
            assert ran and not confirmer.asked
        elif available:
            assert ran == (answer == "allow"), result
            assert confirmer.asked  # the person was asked
        else:
            assert ran == trusted, result  # headless: only a locally trusted core
            if not trusted:
                assert result["_nox"]["refused"] == "high_needs_local_confirmation"
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


def _rank(risk: str) -> int:
    return ("read", "low", "medium", "high", "critical").index(risk)


async def test_the_node_says_whether_it_can_ask(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    for available in (True, False):
        node = await start_node(
            listener, pairing, tls, tmp_path, confirmer=Confirmer(available=available)
        )
        try:
            session = await session_of(listener, node.node_id)
            assert session.hello.confirmer is available
        finally:
            node.stop.set()
            await asyncio.wait_for(node.task, 5)


@pytest.mark.parametrize(
    "value",
    [
        "<b>klick</b> &amp; <a href='x'>hier</a>",  # markup is text
        "C:\\Users\\Beispiel\\Downloads\\" + "x" * 300,  # a long path, in full
        "rm\x1b[2J\r\nAllow: yes\u202e",  # control and bidi characters
    ],
)
def test_the_question_shows_everything_or_nothing(value: str) -> None:
    text = confirmation_text("package.install", {"name": value})
    assert text is not None
    assert "\x1b" not in text and "\r" not in text and "\u202e" not in text
    escaped = value.replace("\x1b", "\\x1b").replace("\r", "\\x0d").replace("\n", "\\x0a")
    escaped = escaped.replace("\u202e", "\\u202e")
    assert escaped in text  # in full - nothing cut silently
    assert confirmation_text("package.install", {"name": "y" * 5000}) is None  # too long: refuse


async def test_too_long_to_confirm_is_refused_not_cut(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    confirmer = Confirmer("allow")
    node = await start_node(listener, pairing, tls, tmp_path, confirmer=confirmer)
    try:
        session = await session_of(listener, node.node_id)
        result = await call(
            session,
            listener.signer,
            "system.high",
            {"target": "z" * 5000},
            risk="high",  # type: ignore[attr-defined]
        )
        assert result["_nox"]["refused"] == "too_long_to_confirm"
        assert not confirmer.asked and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_a_broken_dialog_is_a_no(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    class Broken(Confirmer):
        async def ask(self, text: str, *, timeout_s: int = 60) -> str:
            raise RuntimeError("no display")

    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Broken())
    try:
        session = await session_of(listener, node.node_id)
        result = await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        assert result["_nox"]["refused"] == "denied_on_device" and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


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


async def test_a_changed_receipt_key_is_never_accepted(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    other = ReceiptSigner(tmp_path / "other")
    node = await start_node(listener, pairing, tls, tmp_path, receipt_key=other.public_key)
    try:
        await asyncio.sleep(1.0)
        # The core answers with its own key, not the pinned one: the node never serves it.
        assert not node.agent.connected.is_set()
        assert node.agent.attempts >= 1
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_an_expired_certificate_ends_the_session(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Review N1: a session does not outlive its certificate."""
    from nox.node.repo import NodeRepository

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


async def test_the_stop_reaches_five_nodes_in_under_a_second(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Spec C §1.7: p99 of engage -> every node's nox/killed < 1 s, with a long call running on
    each node; no call completes after its node said killed."""
    nodes = [await start_node(listener, pairing, tls, tmp_path) for _ in range(5)]
    try:
        sessions = [await session_of(listener, n.node_id) for n in nodes]
        durations: list[float] = []
        for _round in range(10):
            running = [
                asyncio.create_task(call(s, listener.signer, "system.wait", timeout=30))  # type: ignore[attr-defined]
                for s in sessions
            ]
            for _ in range(200):
                if all(n.ran.count("wait-start") > _round for n in nodes):
                    break
                await asyncio.sleep(0.01)
            started = time.monotonic()
            acked = await listener.broadcast_kill("latency test")
            durations.append(time.monotonic() - started)
            assert all(acked.values()), acked
            results = await asyncio.gather(*running)
            assert all(r["_nox"]["refused"] == "stopped" for r in results)
            await listener.broadcast_resume()
            for _ in range(200):
                if all(n.agent.stopped_reason() is None for n in nodes):
                    break
                await asyncio.sleep(0.01)
        assert not any("wait-end" in n.ran for n in nodes)
        p99 = sorted(durations)[max(0, int(len(durations) * 0.99) - 1)]
        assert p99 < 1.0, (p99, statistics.mean(durations))
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


def test_here_from_a_browser_on_a_node_means_that_node() -> None:
    """Review N5."""
    from nox.node.registry import NodeEntry
    from nox.node.router import resolve

    laptop = NodeEntry(
        device_id="a" * 32,
        name="Laptop",
        online=True,
        tools={"notify.show": "low"},
        linked_device_id="d" * 32,
    )
    other = NodeEntry(device_id="b" * 32, name="PC", online=True, tools={"notify.show": "low"})
    found = resolve(
        "notify.show",
        "hier",
        entries=[laptop, other],
        core_has=False,
        side_effects=True,
        default=None,
        origin="d" * 32,
    )
    assert found.device == "a" * 32
    core = resolve(
        "system.info",
        "hier",
        entries=[laptop],
        core_has=True,
        side_effects=False,
        default=None,
        origin=None,
    )
    assert core.device == "core"


async def test_the_core_delegates_only_to_a_node_that_can_ask() -> None:
    from nox.node.registry import NodeEntry
    from nox.node.router import routed_spec
    from nox.node.tools import NotifyArgs
    from nox.security.model import Risk

    class Registry:
        def __init__(self, entries: list[NodeEntry]) -> None:
            self._e = entries

        def entries(self) -> list[NodeEntry]:
            return self._e

    for confirmer in (True, False):
        entry = NodeEntry(
            device_id="a" * 32,
            name="Laptop",
            online=True,
            tools={"notify.show": "high"},
            confirmer=confirmer,
        )
        spec = routed_spec(
            name="notify.show",
            description="t",
            risk=Risk.LOW,
            side_effects=True,
            registry=Registry([entry]),
            input_model=NotifyArgs,  # type: ignore[arg-type]
        )
        payload = spec.input_model.model_validate({"text": "x"}).model_dump()
        assert spec.scope is not None
        scope = spec.scope(payload)
        assert scope.risk is Risk.HIGH and scope.confirm_on_device is confirmer


async def test_the_executor_skips_its_own_prompt_only_when_delegated() -> None:
    from nox.security.model import Decision, PermissionResult, Risk
    from nox.tools.executor import ToolExecutor
    from nox.tools.registry import CallScope, ToolRegistry, ToolSpec

    class Engine:
        def __init__(self, decision: Decision) -> None:
            self.decision, self.prompts = decision, 0

        def check(self, _request: Any) -> PermissionResult:
            return PermissionResult(decision=self.decision, rule_id="t")

        def request_confirmation(self, _request: Any) -> str:
            self.prompts += 1
            return "g"

        async def await_confirmation(
            self, _gid: str, *, timeout: float | None = None
        ) -> PermissionResult:
            return PermissionResult(decision=Decision.DENY, rule_id="t")

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    class Kill:
        def is_engaged(self) -> bool:
            return False

    async def handler(_p: dict[str, Any]) -> dict[str, Any]:
        return {"ran": True}

    for delegated, device, decision, prompts, ok in [
        (True, "a" * 32, Decision.CONFIRM, 0, True),  # left to the device
        (True, "", Decision.CONFIRM, 1, False),  # not a node: the core asks
        (False, "a" * 32, Decision.CONFIRM, 1, False),  # not delegated: the core asks
        (True, "a" * 32, Decision.DENY, 0, False),  # the core's refusal stands
    ]:
        tools = ToolRegistry()
        tools.register(
            ToolSpec(
                name="package.install",
                description="t",
                input_model=_Args,
                risk=Risk.HIGH,
                handler=handler,
                scope=lambda _p, d=device, g=delegated: CallScope(
                    action="install", risk=Risk.HIGH, device=d, confirm_on_device=g
                ),
            )
        )
        engine = Engine(decision)
        executor = ToolExecutor(tools, engine, Audit(), None, Kill())  # type: ignore[arg-type]
        result = await executor.call("companion", "package.install", {}, mode="companion")
        assert engine.prompts == prompts and result.ok is ok, (delegated, device, decision)


_ = (GROUPS,)


# ---- found in the self-review ------------------------------------------------------------------


async def test_a_node_that_connects_while_nox_is_stopped_stays_stopped(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    from nox.node.repo import NodeRepository

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


async def test_a_flood_of_calls_is_capped(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        running = [
            asyncio.create_task(call(session, listener.signer, "system.wait", timeout=30))  # type: ignore[attr-defined]
            for _ in range(agent_module.MAX_CONCURRENT_CALLS)
        ]
        for _ in range(200):
            if node.ran.count("wait-start") == agent_module.MAX_CONCURRENT_CALLS:
                break
            await asyncio.sleep(0.02)
        extra = await call(session, listener.signer, "system.info", risk="read")  # type: ignore[attr-defined]
        assert extra["_nox"]["refused"] == "busy"
        await listener.broadcast_kill("test")
        await asyncio.gather(*running)
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_a_stop_closes_an_open_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node import confirm as confirm_module

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
    gate = NodeGate(_tools([]), NodeManifest(), None)
    agent = NodeAgent(home, NodeKeyStore(home.key_file, None), gate, None, confirmer=Confirmer())
    assert agent.stopped_reason() is None
    home.stop_file.write_text("stop")
    assert agent.stopped_reason() == "stopped_on_device"
    assert gate.check("system.info", {}).reason == "stopped_on_device"
