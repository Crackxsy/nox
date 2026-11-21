"""The node's own limits in `NodeGate`: the core may not understate a risk, the node keeps
its own budget (also across a restart), `max_risk` lowers the ceiling, and a side effect
without a receipt key is refused."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.node.device.agent import node_agent as agent_module
from nox.node.device.gate import NodeGate
from nox.node.device.tools import NodeTool
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier
from nox.security.audit import SqliteAuditLog
from tests.unit.node.support import (
    call,
    call_meta,
    gate_with_every_group,
    scripted_tools,
    session_of,
    signed,
    start_node,
)

# ---- the gate's own limits ---------------------------------------------------------------------


def _gate(manifest: NodeManifest | None = None, *, signer: ReceiptSigner) -> NodeGate:
    gate = NodeGate(scripted_tools([]), manifest or NodeManifest(), None)
    gate.receipts = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
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
    gate = NodeGate(scripted_tools([]), NodeManifest(), None)
    signer = ReceiptSigner(tmp_path / "r")
    assert gate.check("system.low", {}, meta=_meta(signer, "system.low", {})).reason == (
        "receipt_missing"
    )


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


# ---- LOW ---------------------------------------------------------------------------------------


def test_a_restart_does_not_refill_the_budget(tmp_path: Path) -> None:
    """N1, and the budget charges the higher of the node's and the core's risk."""
    audit = SqliteAuditLog(sqlite3.connect(tmp_path / "audit.db", check_same_thread=False))
    manifest = NodeManifest(
        groups={g: True for g in GROUPS}, max_calls_per_hour=3, max_high_per_hour=1
    )
    gate, signer = gate_with_every_group(tmp_path, manifest, audit)
    args = {"text": "y"}
    # A low tool the core calls high counts against the high budget.
    assert gate.check(
        "system.low", args, meta=call_meta(signer, "system.low", args, risk="high")
    ).allowed
    assert gate.check("system.low", args, meta=call_meta(signer, "system.low", args)).allowed
    restarted, _ = gate_with_every_group(tmp_path, manifest, audit)
    restarted.receipts = gate.receipts
    restarted.restore_budget(audit)
    assert (
        restarted.check(
            "system.low", args, meta=call_meta(signer, "system.low", args, risk="high")
        ).reason
        == "budget"
    )  # the one high call of the hour was already spent
    assert restarted.check("system.low", args, meta=call_meta(signer, "system.low", args)).allowed
    assert (
        restarted.check("system.low", args, meta=call_meta(signer, "system.low", args)).reason
        == "budget"
    )


@pytest.mark.parametrize("core_risk", ["high", "critical"])
def test_critical_from_the_core_is_never_cheaper_or_unconfirmed(
    tmp_path: Path, core_risk: str
) -> None:
    manifest = NodeManifest(groups={g: True for g in GROUPS}, max_high_per_hour=1)
    gate, signer = gate_with_every_group(tmp_path, manifest)
    args = {"text": "y"}
    first = gate.check(
        "system.low", args, meta=call_meta(signer, "system.low", args, risk=core_risk)
    )
    assert first.allowed and first.needs_local_confirmation and first.charged == "high"
    again = gate.check(
        "system.low", args, meta=call_meta(signer, "system.low", args, risk=core_risk)
    )
    assert again.reason == "budget"  # counted as high


def test_an_unknown_risk_ranks_highest(tmp_path: Path) -> None:
    from nox.node.device.gate import _rank

    assert _rank("extreme") > _rank("critical") > _rank("high")

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def run(_a: BaseModel) -> dict[str, Any]:
        return {}

    gate = NodeGate(
        {"odd.tool": NodeTool("odd.tool", "system", "extreme", "t", Args, run, path_safe=True)},
        NodeManifest(),
        None,
    )
    assert gate.check("odd.tool", {}).reason == "risk_ceiling"  # fail closed


def test_restored_budget_counts_anything_high_or_unknown_as_high(tmp_path: Path) -> None:
    audit = SqliteAuditLog(sqlite3.connect(tmp_path / "a.db", check_same_thread=False))
    for charged in ("critical", "weird"):
        audit.append(
            actor="core",
            tool="x",
            action="node.call",
            target="",
            decision="allow",
            result="ok",
            details={"charged": charged},
        )
    manifest = NodeManifest(groups={g: True for g in GROUPS}, max_high_per_hour=2)
    gate, signer = gate_with_every_group(tmp_path, manifest)
    gate.restore_budget(audit)
    args = {"text": "y"}
    assert (
        gate.check(
            "system.low", args, meta=call_meta(signer, "system.low", args, risk="high")
        ).reason
        == "budget"
    )
