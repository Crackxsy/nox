"""Receipts: the core signs exactly one call for exactly one node, and the node verifies it.

A receipt binds the call id, the device, the core, the tool, the arguments, the risk, who
decided and the deadline; it expires, it is used once, another key signs nothing, and a
changed receipt key is never accepted over the wire."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from nox.lan.tls import TlsMaterial
from nox.node.device.gate import NodeGate
from nox.node.device.hands import NodeHands, hand_tools
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier
from tests.unit.node.support import CORE_ID, DEVICE_ID, gate_with_every_group, signed, start_node

# ---- receipts ----------------------------------------------------------------------------------


def _verifier_and_signer(tmp_path: Path) -> tuple[ReceiptVerifier, ReceiptSigner]:
    signer = ReceiptSigner(tmp_path / "r")
    return ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64), signer


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
        "tainted": meta["tainted"],
        "issued_at": meta["issued_at"],
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
    old = signed(signer, "system.low", {}, issued_at=time.time() - 60, deadline=time.time() - 30)
    assert _check(verifier, old) == "receipt_expired"
    long = signed(signer, "system.low", {}, deadline=time.time() + 600)
    assert _check(verifier, long) == "receipt_too_long"
    future = signed(signer, "system.low", {}, issued_at=time.time() + 300)
    assert _check(verifier, future) == "receipt_from_the_future"
    assert _check(verifier, {**old, "receipt": ""}) == "receipt_missing"


def test_another_key_signs_nothing(tmp_path: Path) -> None:
    verifier, _signer = _verifier_and_signer(tmp_path)
    other = ReceiptSigner(tmp_path / "other")
    assert _check(verifier, signed(other, "system.low", {})) == "receipt_invalid"


def test_clock_skew_is_measured_from_the_cores_pings(tmp_path: Path) -> None:
    signer = ReceiptSigner(tmp_path / "r")
    core_ahead = 300.0
    verifier = ReceiptVerifier(
        signer.public_key,
        device_id="d" * 32,
        core_id="c" * 64,
        clock=lambda: time.time() - core_ahead,
    )
    meta = signed(signer, "system.low", {})
    # The node's clock is 5 min behind: the receipt looks issued in the future.
    assert _check(verifier, meta) == "receipt_from_the_future"
    verifier.observe_core_time(time.time())
    assert _check(verifier, signed(signer, "system.low", {})) is None


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


def test_a_receipt_for_another_node_or_core_is_invalid(tmp_path: Path) -> None:
    gate, signer = gate_with_every_group(tmp_path)
    args = {"text": "y"}
    other_node = NoxCallMeta.model_validate(
        signed(signer, "system.low", args, device_id="e" * 32, core_id=CORE_ID)
    )
    assert gate.check("system.low", args, meta=other_node).reason == "receipt_invalid"
    other_core = NoxCallMeta.model_validate(
        signed(signer, "system.low", args, device_id=DEVICE_ID, core_id="f" * 64)
    )
    assert gate.check("system.low", args, meta=other_core).reason == "receipt_invalid"


def test_one_receipt_is_used_once_even_from_many_threads(tmp_path: Path) -> None:
    import threading

    from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    tools = hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None))
    gate = NodeGate(
        tools, NodeManifest(groups={g: g == "files" for g in GROUPS}), None, receipts=verifier
    )
    args = {"path": "a.txt", "text": "x", "append": False, "overwrite": False}
    meta = NoxCallMeta(
        **signed(signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64)
    )
    results: list[bool] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(gate.check("file.write", args, meta=meta).allowed)
        )
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1  # replay protection holds under concurrency
