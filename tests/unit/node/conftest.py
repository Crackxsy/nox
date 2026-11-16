"""Fixtures for the node tests: a migrated database, a fresh authority per test (in `tmp_path`,
never the real Nox folder), node pairing, and a scripted node that speaks raw JSON-RPC - so a
test can send exactly what a malicious node would."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from nox.data.db import Database
from nox.lan.tls import TlsMaterial, ensure_tls
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.repo import NodeRepository
from nox.remote.repo import RemoteRepository
from tests.unit.ipc.conftest import SimpleBus
from tests.unit.node import support
from tests.unit.node.support import (
    ADDRESSES,
    NAMES,
)


@pytest.fixture(autouse=True)
def _english_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node's terminal and dialog words in English, whatever this machine's locale; the
    German texts are tested on their own (`test_gremium_r1a.py`)."""
    monkeypatch.setenv("NOX_LANG", "en")


@pytest.fixture(autouse=True)
def _instant_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripted confirmers answer at once; the 1.5 s rule against reflex answers is tested on
    its own (`test_review_c4.py`)."""
    from nox.node import agent as agent_module

    monkeypatch.setattr(agent_module, "MIN_DECISION_S", 0.0)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repo(db: Database) -> RemoteRepository:
    return RemoteRepository(db)


@pytest.fixture
def nodes(db: Database) -> NodeRepository:
    return NodeRepository(db)


@pytest.fixture
def tls(tmp_path: Path) -> TlsMaterial:
    return ensure_tls(tmp_path / "tls", names=NAMES, addresses=ADDRESSES)


@pytest.fixture
def pairing(
    repo: RemoteRepository, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> NodePairing:
    return NodePairing(repo, nodes, tls_dir=tmp_path / "tls", bus=SimpleBus())  # type: ignore[arg-type]


@pytest.fixture
async def listener(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> AsyncIterator[NodeListener]:
    from nox.node.receipts import ReceiptSigner

    signer = ReceiptSigner(tmp_path / "tls")
    kills: list[tuple[str, str]] = []

    async def on_kill(device_id: str, reason: str) -> None:
        kills.append((device_id, reason))

    server = NodeListener(
        pairing=pairing,
        nodes=nodes,
        tls=lambda: tls,
        host="127.0.0.1",
        port=0,
        on_security_kill=on_kill,
        ping_interval_s=3600,
        receipt_key=signer.public_key,
    )
    server.kills = kills  # type: ignore[attr-defined]
    server.signer = signer  # type: ignore[attr-defined]
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture(autouse=True)
def _same_groups_on_every_test_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the macOS runner `shell` and `media` are not available (`cli_text.supported_here`), so
    tests that switch them on would fail there for a reason they are not about. They see the
    groups of Linux; a test about the platform sets its own answer."""
    import sys

    if sys.platform != "darwin":
        return
    from nox.node import cli, cli_text

    real = cli_text.unsupported_reason
    monkeypatch.setattr(
        cli, "unsupported_reason", lambda group, platform="linux": real(group, platform)
    )
    monkeypatch.setattr(
        cli, "supported_here", lambda group, platform="linux": real(group, platform) is None
    )


@pytest.fixture(autouse=True)
def receipt_target(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The node and core the receipts of this test are signed for - a fresh one for every test.

    `support.signed` reads it, `support.start_node` points it at the core of the node it starts.
    Nothing of it outlives the test."""
    target = {"device": "d" * 32, "core": "c" * 64}
    monkeypatch.setattr(support, "_receipt_target", target)
    return target


@pytest.fixture
def a_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device with a screen (`mode: auto` is push-to-talk there). The satellite test modules
    use it for every test; the headless default has a test of its own
    (`test_a_headless_device_listens_for_the_wake_word`)."""
    monkeypatch.setattr("nox.node.confirm.has_screen", lambda: True)


@pytest.fixture
def fs_world(tmp_path: Path) -> dict[str, Path]:
    """An allowed folder with one note in it, the node folder with its key, and a folder
    outside both - the three places a path on a node can point to."""
    root = tmp_path / "allowed"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "einkauf.txt").write_bytes(b"Haferflocken\n")
    node = tmp_path / "node-home"
    node.mkdir()
    (node / "node.key").write_text("NODE-KEY", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "private.txt").write_text("not for Nox", encoding="utf-8")
    return {"root": root, "node": node, "outside": outside}


@pytest.fixture
def empty_fs_world(tmp_path: Path) -> dict[str, Path]:
    """An allowed folder with an empty `notes` folder and an empty node folder: what the write
    tests start from, so that a file found afterwards was written by the test."""
    root = tmp_path / "allowed"
    (root / "notes").mkdir(parents=True)
    node = tmp_path / "node-home"
    node.mkdir()
    return {"root": root, "node": node}
