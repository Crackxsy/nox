"""Spec C §1.7 with real processes: five nodes, each in a Python process of its own (its own node
folder and Nox folder), each running a real `sleep 60` in its real sandbox (bwrap) through the
real `NodeAgent`, `NodeGate` and `shell.run`. The core side is a real `NodeListener` in this test
process; the kill switch (`KillSwitchService`, with the listener as its "nodes" stop hook, wired
as `nox.node.install` wires it) is engaged, and each round is timed from `engage()` until the
last node's `nox/killed` arrived: the p99 stays under a second, every call is answered
`stopped`, and no sandboxed process outlives the kill.

Linux only, where a strong sandbox exists (bwrap). CI's `python-linux` job installs bubblewrap
and sets `NOX_REQUIRE_BWRAP=1`, so a missing or broken bwrap fails there instead of skipping.
The person at each device is scripted (allows at once); nothing is shown, played or recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest

from nox.data.db import Database
from nox.lan.tls import TlsMaterial, ensure_tls
from nox.node.home import HOME_ENV, NodeHome, NodeIdentity
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.receipts import ReceiptSigner
from nox.node.repo import NodeRepository
from nox.remote.repo import RemoteRepository
from tests.unit.ipc.conftest import SimpleBus
from tests.unit.node.conftest import ADDRESSES, NAMES, key_pem, pair_node
from tests.unit.node.test_c4 import call, kill_switch_for, percentile, session_of

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the strong sandbox (bwrap)"),
    pytest.mark.timeout(180),
]

NODES = 5
ROUNDS = 5
#: How long a child may take to build and probe its sandbox, and a sandboxed run to start.
START_S = 60.0
RUN_START_S = 20.0
#: How long a killed run's process tree may take to be gone.
GONE_S = 5.0

#: One node: the real hands with this device's real sandbox, `shell` allowed here, a scripted
#: person who allows at once (the 1.5 s rule against reflex answers is tested elsewhere). Prints
#: READY once its sandbox works (NOSANDBOX and why otherwise); ends when stdin closes.
CHILD = r"""
import asyncio, os, sqlite3, sys, threading
from pathlib import Path

from nox.node import agent as agent_module
from nox.node.agent import NodeAgent
from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, default_sandbox, hand_tools
from nox.node.home import NodeHome
from nox.node.keystore import NodeKeyStore
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.tools import NodeRefusal, default_tools
from nox.security.audit import SqliteAuditLog

agent_module.MIN_DECISION_S = 0.0


class Allow:
    available = True

    async def ask(self, text, *, timeout_s=60):
        return "allow"


async def main():
    home = NodeHome(Path(sys.argv[1]))
    hands = NodeHands(folders=list, protected=(home.path,), sandbox=default_sandbox)
    try:
        await hands.confined()  # built and probed once, before anything is asked of it
    except NodeRefusal as exc:
        print("NOSANDBOX", exc.reason, repr(exc.__cause__), flush=True)
        return
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    manifest = NodeManifest(
        groups={g: g in ("system", "shell") for g in GROUPS},
        max_calls_per_hour=1000,
        max_high_per_hour=100,
    )
    tools = {**default_tools(None), **hand_tools(hands)}
    gate = NodeGate(tools, manifest, audit, protected=home.path)
    agent = NodeAgent(
        home, NodeKeyStore(home.key_file, None), gate, audit, ping_interval_s=3600,
        confirmer=Allow(),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def watch():
        sys.stdin.read()
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=watch, daemon=True).start()
    print("READY", os.getpid(), flush=True)
    outcome = await agent.run(stop)
    print("DONE", outcome, flush=True)


asyncio.run(main())
"""


@dataclass
class Child:
    node_id: str
    process: subprocess.Popen[bytes]
    log: Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def descendants(self) -> list[psutil.Process]:
        """What runs below this node's process right now (zombies are already gone)."""
        try:
            found = psutil.Process(self.process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return []
        alive: list[psutil.Process] = []
        for process in found:
            with contextlib.suppress(psutil.NoSuchProcess):
                if process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
        return alive

    def sleeping(self, seen: set[tuple[int, float]]) -> bool:
        """Whether its sandboxed `sleep 60` runs (inside bwrap's tree below the node). Every
        process seen below the node goes into `seen`, so the end can check none outlived it."""
        running = False
        for process in self.descendants():
            with contextlib.suppress(psutil.Error):
                seen.add((process.pid, process.create_time()))
                if process.name() == "sleep" and process.cmdline()[-1:] == ["60"]:
                    running = True
        return running


def _alive(seen: set[tuple[int, float]]) -> list[int]:
    """The processes of `seen` that still run (the same process: same pid, same start)."""
    left: list[int] = []
    for pid, created in seen:
        with contextlib.suppress(psutil.Error):
            process = psutil.Process(pid)
            if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
                left.append(pid)
    return left


@pytest.fixture
def strong_sandbox() -> None:
    from nox.sandbox.linux import bwrap_available

    if not bwrap_available():
        if os.environ.get("NOX_REQUIRE_BWRAP") == "1":
            pytest.fail("bwrap must be there in this job")
        pytest.skip("no bwrap here: no strong sandbox for a node's shell.run")


@pytest.fixture
def tls(tmp_path: Path) -> TlsMaterial:
    return ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def pairing(database: Database, tls: TlsMaterial, tmp_path: Path) -> NodePairing:
    return NodePairing(
        RemoteRepository(database),
        NodeRepository(database),
        tls_dir=tmp_path / "tls",
        bus=SimpleBus(),
    )


@pytest.fixture
async def listener(
    pairing: NodePairing, database: Database, tls: TlsMaterial, tmp_path: Path
) -> AsyncIterator[NodeListener]:
    signer = ReceiptSigner(tmp_path / "tls")
    server = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(database),
        tls=lambda: tls,
        host="127.0.0.1",
        port=0,
        # Pinged often: a run lasts longer than the nodes' dead-man rule (3 s of silence).
        ping_interval_s=0.5,
        receipt_key=signer.public_key,
    )
    server.signer = signer  # type: ignore[attr-defined]
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _spawn(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path, index: int
) -> Child:
    paired = await pair_node(pairing, tmp_path)
    home = NodeHome(tmp_path / f"node-home-{index}")
    home.ensure()
    home.key_file.write_bytes(key_pem(paired.key))
    home.save_pairing(
        NodeIdentity(
            node_id=paired.node_id,
            name=f"Testgeraet {index}",
            core_host="127.0.0.1",
            core_port=47843,
            node_port=listener.port,
            ca_fingerprint=tls.fingerprint.replace(":", "").lower(),
            paired_at=datetime.now(UTC),
        ),
        cert_pem=paired.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    return _start(paired.node_id, home, tmp_path, index)


def _start(node_id: str, home: NodeHome, tmp_path: Path, index: int) -> Child:
    """The node's own process, with its own node folder and Nox folder; its output in a file."""
    nox_home = tmp_path / f"nox-home-{index}"
    nox_home.mkdir()
    log = tmp_path / f"node-{index}.log"
    env = {
        **os.environ,
        "NOX_HOME": str(nox_home),
        HOME_ENV: str(home.path),
        "PYTHONUNBUFFERED": "1",
    }
    with log.open("wb") as out:
        process = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed script
            [sys.executable, "-c", CHILD, str(home.path)],
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=tmp_path,
        )
    return Child(node_id, process, log)


def _run_started(
    child: Child, running: list[asyncio.Task[dict[str, Any]]], seen: set[tuple[int, float]]
) -> bool:
    """Whether the node's sandboxed `sleep 60` runs; a call answered before that fails the test
    with what it said."""
    for task in running:
        if task.done():
            pytest.fail(f"a call ended before the kill: {task.result()}\n{child.output()[-2000:]}")
    return child.sleeping(seen)


def _last_words(child: Child, what: str) -> str:
    return f"{what}\n{child.output()[-2000:]}"


async def _until(check: Callable[[], bool], seconds: float, what: str | Callable[[], str]) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() >= deadline:
            pytest.fail(what if isinstance(what, str) else what())
        await asyncio.sleep(0.05)


def _sleeps_started_since(started: float) -> list[psutil.Process]:
    """`sleep 60` processes of this user that began during the test - none may be left."""
    me = psutil.Process().username()
    found: list[psutil.Process] = []
    for process in psutil.process_iter(["name", "cmdline", "create_time", "username"]):
        info = process.info
        with contextlib.suppress(psutil.Error, TypeError):
            if (
                info["name"] == "sleep"
                and (info["cmdline"] or [])[-1:] == ["60"]
                and info["create_time"] >= started - 1
                and info["username"] == me
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                found.append(process)
    return found


async def test_the_kill_switch_stops_five_node_processes_and_their_sandboxed_runs(
    strong_sandbox: None,
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
) -> None:
    began = time.time()
    switch, all_killed, acks = kill_switch_for(listener)
    core_id = tls.fingerprint.replace(":", "").lower()
    children: list[Child] = []
    seen: set[tuple[int, float]] = set()
    try:
        for index in range(NODES):
            children.append(await _spawn(listener, pairing, tls, tmp_path, index))
        for child in children:

            def ready(child: Child = child) -> bool:
                text = child.output()
                if "NOSANDBOX" in text or child.process.poll() is not None:
                    pytest.fail(f"a node has no working sandbox:\n{text[-2000:]}")
                return "READY" in text

            await _until(ready, START_S, "a node never got ready")
        sessions = [await session_of(listener, child.node_id) for child in children]
        durations: list[float] = []
        for round_ in range(ROUNDS):
            running = [
                asyncio.create_task(
                    call(
                        session,
                        listener.signer,  # type: ignore[attr-defined]
                        "shell.run",
                        {"argv": ["sleep", "60"], "egress": []},
                        risk="high",
                        confirm_on_device=True,
                        core_id=core_id,
                        timeout=40,
                    )
                )
                for session in sessions
            ]
            for child in children:
                await _until(
                    functools.partial(_run_started, child, running, seen),
                    RUN_START_S,
                    functools.partial(_last_words, child, f"round {round_}: no sandboxed run"),
                )
            engaged = time.monotonic()
            await switch.engage("ui", "kill test with node processes")
            durations.append(all_killed[-1] - engaged)
            assert len(acks[-1]) == NODES and all(acks[-1].values()), acks[-1]
            assert all(session.killed.is_set() for session in sessions)
            results = await asyncio.wait_for(asyncio.gather(*running), 10)
            assert all(r["_nox"]["refused"] == "stopped" for r in results), results
            await _until(
                lambda: not any(child.descendants() for child in children),
                GONE_S,
                lambda: f"a sandboxed process outlived the kill: {_alive(seen)}",
            )
            assert await switch.resume(pin_ok=False, by="test")
            await asyncio.sleep(0.3)  # the resume reached every node before the next round
        p99 = percentile(durations, 99)
        assert p99 < 1.0, (p99, statistics.mean(durations), durations)
        assert not _sleeps_started_since(began)
    finally:
        for child in children:
            with contextlib.suppress(OSError):
                assert child.process.stdin is not None
                child.process.stdin.close()  # the node's stop: its agent ends
        exits: list[int | None] = []
        for child in children:
            try:
                exits.append(await asyncio.to_thread(child.process.wait, 15))
            except subprocess.TimeoutExpired:
                for process in child.descendants():
                    with contextlib.suppress(psutil.Error):
                        process.kill()
                child.process.kill()
                await asyncio.to_thread(child.process.wait, 5)
                exits.append(None)
        leftovers = _alive(seen)
        for pid in leftovers:  # never left running, whatever the test says
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).kill()
        for process in _sleeps_started_since(began):
            with contextlib.suppress(psutil.Error):
                process.kill()
    outputs = [(code, child.output()[-1000:]) for code, child in zip(exits, children, strict=True)]
    assert exits == [0] * NODES, outputs
    assert not leftovers, leftovers
