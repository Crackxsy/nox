"""Shared helpers for the tests of a node's hands (`file.*`, `shell.run`): folders on a
temporary disk, reads and writes that say why they were refused, a scripted sandbox, and
a node with hands running against the real listener."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.device.agent import NodeAgent
from nox.node.device.gate import NodeGate
from nox.node.device.hands.args import WriteArgs
from nox.node.device.hands.catalogue import hand_tools
from nox.node.device.hands.node_hands import NodeHands
from nox.node.device.home import NodeHome, NodeIdentity
from nox.node.device.keystore import NodeKeyStore
from nox.node.device.safe_fs.core import OutsideFolders
from nox.node.device.safe_fs.folders import Folders
from nox.node.device.safe_fs.opening import open_existing
from nox.node.device.tools import NodeRefusal
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.sandbox import Result, SandboxMode, SandboxUnavailable
from nox.security.audit import SqliteAuditLog
from tests.unit.node.support import Confirmer, current_receipt_target, key_pem, pair_node

WINDOWS = sys.platform == "win32"


def link_folder(target: Path, link: Path) -> None:
    """A link to a folder the way this OS offers one without privileges."""
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def folders(world: dict[str, Path]) -> Folders:
    """The allowed and protected folders of `world`, as the node builds them."""
    return Folders.build([str(world["root"])], [world["node"]])


def read_file(f: Folders, text: str) -> bytes:
    """Open `text` for reading under `f` and return the first bytes."""
    fd = open_existing(f, text)
    try:
        return os.read(fd, 1000)
    finally:
        os.close(fd)


def read_refusal(f: Folders, text: str) -> str:
    """The reason why reading `text` is refused (the read must be refused)."""
    with pytest.raises(OutsideFolders) as caught:
        read_file(f, text)
    return caught.value.reason


# ---- the file tools ----------------------------------------------------------------------------


def hand_tools_for(world: dict[str, Path], sandbox: Any = None) -> dict[str, Any]:
    """The hand tools of a node whose only allowed folder is the world's root."""
    hands = NodeHands(
        folders=lambda: [str(world["root"])], protected=(world["node"],), sandbox=lambda: sandbox
    )
    # The same catalogue on every OS the tests run on (macOS withholds `shell.run` itself).
    return hand_tools(hands, platform="linux", trash=lambda: None)


# ---- shell and packages: only in the sandbox -----------------------------------------------------


class FakeSandbox:
    """Records what would run; strong unless told otherwise."""

    mode = SandboxMode.LINUX_BWRAP

    def __init__(self, *, weak: bool = False, sleep: float = 0.0) -> None:
        self.runs: list[tuple[list[str], tuple[str, ...], str]] = []
        self.weak, self.sleep, self.cancelled = weak, sleep, False

    def check_usable(self) -> None:
        if self.weak:
            raise SandboxUnavailable("weak")

    async def run(self, argv: list[str], *, egress: Any, limits: Any, workload: str) -> Result:
        self.runs.append((argv, egress.hosts, workload))
        try:
            await asyncio.sleep(self.sleep)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Result(exit_code=0, stdout="ok\n", stderr="")


# ---- through the gate, the confirmation and the receipts (real listener) -------------------------


async def start_hands_node(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    world: dict[str, Path],
    sandbox: Any,
    confirmer: Confirmer,
) -> tuple[NodeAgent, asyncio.Task[str], asyncio.Event, SqliteAuditLog]:
    """A paired node with real hands (files, shell) running against the listener over TLS."""
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(world["node"])
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
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    current_receipt_target()["core"] = tls.fingerprint.replace(":", "").lower()
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    groups = {g: g in ("system", "files", "shell", "packages") for g in GROUPS}
    manifest = NodeManifest(groups=groups, folders=[str(world["root"])])
    holder: list[NodeGate] = []
    hands = NodeHands(
        folders=lambda: list(holder[0].manifest.folders),
        protected=(home.path,),
        sandbox=lambda: sandbox,
    )
    gate = NodeGate(hand_tools(hands, platform="linux"), manifest, audit, protected=home.path)
    holder.append(gate)
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        gate,
        audit,
        ping_interval_s=3600,
        confirmer=confirmer,
    )
    stop = asyncio.Event()
    return agent, asyncio.create_task(agent.run(stop)), stop, audit


def write_tools(root: Path, node: Path) -> dict[str, Any]:
    """The hand tools of a node that allows `root` and protects `node`."""
    hands = NodeHands(folders=lambda: [str(root)], protected=(node,), sandbox=lambda: None)
    return hand_tools(hands)


async def write_file(world: dict[str, Path], path: str, text: str = "x", **kw: Any) -> Any:
    """Call `file.write` the way the node's handler is called."""
    tool = write_tools(world["root"], world["node"])["file.write"]
    return await tool.handler(WriteArgs(path=path, text=text, **kw))


async def write_refusal(world: dict[str, Path], path: str, text: str = "x", **kw: Any) -> str:
    """The reason why a write is refused; asserts that nothing was created."""
    with pytest.raises(NodeRefusal) as caught:
        await write_file(world, path, text, **kw)
    assert not (world["root"] / path).exists()
    return caught.value.reason
