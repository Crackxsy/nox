"""`shell.run` and the other hands of a node: offered only with their group, run only in a
strong sandbox, and over the wire each one passes the gate, the dialog and the receipt."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.confirmed import MOST_RESTRICTIVE, effective_manifest, record_confirmed
from nox.node.gate import NodeGate
from nox.node.listener import NodeListener
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.registry import ROUTABLE
from nox.node.tools import NodeRefusal
from nox.security.audit import SqliteAuditLog
from tests.unit.node.support import Confirmer, call, session_of, signed
from tests.unit.node.support_hands import FakeSandbox, hand_tools_for, link_folder, start_hands_node


async def test_shell_is_refused_without_a_strong_sandbox(fs_world: dict[str, Path]) -> None:
    from nox.node.hands import ShellArgs

    for sandbox in (None, FakeSandbox(weak=True)):
        tools = hand_tools_for(fs_world, sandbox)
        with pytest.raises(NodeRefusal) as caught:
            await tools["shell.run"].handler(ShellArgs(argv=["whoami"]))
        assert caught.value.reason == "sandbox_unavailable"
        if sandbox is not None:
            assert sandbox.runs == []  # nothing ran, also not "less confined"


async def test_shell_runs_in_the_sandbox(fs_world: dict[str, Path]) -> None:
    from nox.node.hands import ShellArgs

    sandbox = FakeSandbox()
    tools = hand_tools_for(fs_world, sandbox)
    out = await tools["shell.run"].handler(ShellArgs(command="df -h /"))
    assert out["ok"] and out["stdout"] == "ok\n"
    ((shell_argv, shell_egress, shell_work),) = sandbox.runs
    assert shell_argv[-1] == "df -h /" and shell_egress == () and shell_work == "shell"


def test_packages_are_not_offered_on_nodes_yet(fs_world: dict[str, Path]) -> None:
    assert not {"package.install", "package.remove"} & (set(hand_tools_for(fs_world)) | ROUTABLE)


def test_the_default_sandbox_never_accepts_the_weak_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import nox.sandbox
    from nox.node.hands import default_sandbox

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(nox.sandbox, "make_sandbox", lambda **kw: seen.append(kw) or "made")
    assert default_sandbox() == "made"
    assert seen == [{"accept_weak": False}]


def test_every_hand_is_offered_only_with_its_group(fs_world: dict[str, Path]) -> None:
    tools = hand_tools_for(fs_world)
    assert set(tools) <= ROUTABLE
    gate = NodeGate(tools, NodeManifest(), None, protected=fs_world["node"])
    assert gate.offered() == []  # files and shell are off by default
    on = {g: g in ("files", "shell") for g in GROUPS}
    gate.set_manifest(NodeManifest(groups=on))
    assert {t.name for t in gate.offered()} == {
        "file.list",
        "file.read",
        "file.write",
        "file.move",
        "file.delete",
        "shell.run",
    }


def test_folders_count_only_as_confirmed_on_the_device(tmp_path: Path) -> None:
    from nox.node.confirmed import manifest_key

    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    key = manifest_key(tmp_path, None)
    confirmed = NodeManifest(folders=[str(tmp_path / "a")])
    assert record_confirmed(audit, confirmed, key, create=True)
    edited = NodeManifest(folders=[str(tmp_path / "a"), str(tmp_path / "b")])
    assert effective_manifest(edited, audit, key).folders == [str(tmp_path / "a")]
    assert MOST_RESTRICTIVE.folders == []
    with pytest.raises(ValueError):
        NodeManifest(folders=["relative/path"])
    with pytest.raises(ValueError):
        NodeManifest(folders=["\\\\fileserver\\share"])


async def test_over_the_wire_every_hand_passes_gate_dialog_and_receipt(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    fs_world: dict[str, Path],
) -> None:
    sandbox = FakeSandbox()
    confirmer = Confirmer("allow")
    agent, task, stop, audit = await start_hands_node(
        listener, pairing, tls, tmp_path, fs_world, sandbox, confirmer
    )
    signer = listener.signer  # type: ignore[attr-defined]
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        # A read needs no receipt and no dialog.
        listed = await call(
            session, signer, "file.read", {"path": "notes/einkauf.txt"}, risk="read"
        )
        assert "Haferflocken" in listed["content"][0]["text"]
        # The node folder by name: refused by the gate's text check before any handler.
        home_by_name = await call(
            session, signer, "file.read", {"path": str(fs_world["node"] / "node.key")}, risk="read"
        )
        assert home_by_name["_nox"]["refused"] == "node_home_protected"
        # The node folder through a link: refused by the hand on the handle, and audited.
        link_folder(fs_world["node"], fs_world["root"] / "door")
        door = await call(session, signer, "file.read", {"path": "door/node.key"}, risk="read")
        assert door["_nox"]["refused"] in ("node_home_protected", "link_refused")
        # High risk: asked on this device, then run - in the sandbox.
        args = {"argv": ["whoami"], "egress": []}
        ran = await call(session, signer, "shell.run", args, risk="high", confirm_on_device=True)
        assert ran["structuredContent"]["ok"] is True and len(confirmer.asked) == 1
        # Without the core's receipt nothing with a side effect runs.
        other = {"argv": ["id"], "egress": []}
        meta = signed(signer, "shell.run", other, risk="high", device_id=session.device_id)
        params = {"name": "shell.run", "arguments": args, "_nox": meta}
        forged = await session.client.request("tools/call", params, timeout_s=10)
        assert forged["_nox"]["refused"] == "receipt_invalid"
        assert len(sandbox.runs) == 1
        actions = [e.action for e in audit.entries(since_seq=0, limit=200)]
        assert "node.refused" in actions
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_no_sandbox_on_the_device_means_refused_over_the_wire(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    fs_world: dict[str, Path],
) -> None:
    agent, task, stop, _audit = await start_hands_node(
        listener, pairing, tls, tmp_path, fs_world, None, Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        answer = await call(
            session,
            listener.signer,  # type: ignore[attr-defined]
            "shell.run",
            {"argv": ["whoami"], "egress": []},
            risk="high",
            confirm_on_device=True,
        )
        assert answer["_nox"]["refused"] == "sandbox_unavailable"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_a_kill_from_the_core_stops_the_sandboxed_run(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    fs_world: dict[str, Path],
) -> None:
    sandbox = FakeSandbox(sleep=60)
    agent, task, stop, _audit = await start_hands_node(
        listener, pairing, tls, tmp_path, fs_world, sandbox, Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        running = asyncio.create_task(
            call(
                session,
                listener.signer,  # type: ignore[attr-defined]
                "shell.run",
                {"argv": ["sleep", "60"], "egress": []},
                risk="high",
                confirm_on_device=True,
            )
        )
        for _ in range(200):
            if sandbox.runs:
                break
            await asyncio.sleep(0.02)
        await listener.broadcast_kill("test")
        answer = await asyncio.wait_for(running, 5)
        assert answer["_nox"]["refused"] == "stopped"
        assert sandbox.cancelled is True
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
