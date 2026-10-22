"""Certificate renewal before day 30 (spec C §1.2 step 4): over the authenticated connection, for
the paired key only, once per connection, only in the last `RENEW_WINDOW_DAYS` days - and the node
takes a new certificate only from its pinned authority, for itself and its own key."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509

from nox.lan.tls import TlsMaterial, ensure_tls
from nox.node.agent import NodeAgent
from nox.node.gate import NodeGate
from nox.node.home import NodeHome, NodeIdentity
from nox.node.keystore import NodeKeyStore
from nox.node.listener import NodeListener
from nox.node.manifest import NodeManifest
from nox.node.pairing import NodePairing
from nox.node.protocol import RENEW_WINDOW_DAYS
from nox.node.renewal import RenewalRefusedError, accept_renewed, due, store_certificate
from nox.node.repo import NodeRepository
from nox.node.tools import default_tools
from nox.security.audit import SqliteAuditLog
from tests.unit.node.conftest import (
    client_context,
    greet,
    key_pem,
    make_csr,
    make_key,
    open_node,
    pair_node,
    recv_until,
)


async def _connected(listener: NodeListener, node_id: str) -> None:
    for _ in range(250):
        session = listener.sessions.get(node_id)
        if session is not None and session.ready.is_set():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the node never showed up as a session")


@pytest.fixture(autouse=True)
def _renew_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pairing just issued a certificate; the 12 h limit between two has its own test."""
    from nox.node import pairing as pairing_module

    monkeypatch.setattr(pairing_module, "RENEW_MIN_INTERVAL", timedelta(0))


async def _ask(ws: object, csr: str, msg_id: int = 9) -> dict[str, object]:
    await ws.send(  # type: ignore[attr-defined]
        json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "nox/renew", "params": {"csr": csr}})
    )
    return await recv_until(ws, lambda m: m.get("id") == msg_id)  # type: ignore[arg-type]


async def test_a_node_in_its_last_days_gets_a_new_certificate_once(
    listener: NodeListener,
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    session = listener.sessions[node.node_id]
    session.cert_not_after = datetime.now(UTC) + timedelta(days=RENEW_WINDOW_DAYS - 1)

    answer = await _ask(ws, make_csr(node.key))
    result = answer["result"]
    assert isinstance(result, dict)
    cert = accept_renewed(
        str(result["cert"]),
        ca_file=tls.ca_file,
        current_file=node.cert_file,
        key=node.key,
        node_id=node.node_id,
    )
    assert nodes.get_cert(f"{cert.serial_number:x}") is not None  # recorded like the first
    assert session.cert_not_after == cert.not_valid_after_utc
    old = nodes.get_cert(node.serial)
    assert old is not None and old.revoked  # the old serial goes on the denylist

    again = await _ask(ws, make_csr(node.key), msg_id=10)
    assert again["error"]["message"] == "already_renewed"  # type: ignore[index]
    await ws.close()


async def test_a_fresh_certificate_is_not_renewed(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    answer = await _ask(ws, make_csr(node.key))
    assert answer["error"]["message"] == "too_early"  # type: ignore[index]
    await ws.close()


async def test_a_request_for_another_key_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    listener.sessions[node.node_id].cert_not_after = datetime.now(UTC) + timedelta(days=2)
    answer = await _ask(ws, make_csr(make_key()))
    assert answer["error"]["message"] == "refused"  # type: ignore[index]
    await ws.close()


async def test_the_node_accepts_only_its_own_certificate_from_its_authority(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    other = await pair_node(pairing, tmp_path, name="Desk")
    foreign = ensure_tls(tmp_path / "foreign", names=("localhost",), addresses=("127.0.0.1",))

    def check(cert_pem: str, *, ca: Path = tls.ca_file, node_id: str = node.node_id) -> None:
        accept_renewed(
            cert_pem, ca_file=ca, current_file=node.cert_file, key=node.key, node_id=node_id
        )

    from nox.lan.tls import issue_client_certificate

    shorter = issue_client_certificate(
        tmp_path / "tls", make_csr(node.key), node.node_id, validity_days=5
    )
    for bad in (
        lambda: check(shorter.cert_pem),  # ends before the current one
        lambda: check(other.cert_pem),  # another node's certificate
        lambda: check(node.cert_pem, ca=foreign.ca_file),  # another authority
        lambda: check("not a certificate"),
        # The same key, but a certificate naming another node.
        lambda: check(
            issue_client_certificate(tmp_path / "tls", make_csr(node.key), other.node_id).cert_pem
        ),
    ):
        try:
            bad()
        except RenewalRefusedError:
            continue
        raise AssertionError("accepted a certificate it must refuse")


def test_due_from_day_twenty(tmp_path: Path, tls: TlsMaterial) -> None:
    cert = x509.load_pem_x509_certificate(tls.cert_file.read_bytes())
    path = tmp_path / "node.crt"
    store_certificate(path, tls.cert_file.read_text())
    end = cert.not_valid_after_utc
    assert not due(path, now=end - timedelta(days=RENEW_WINDOW_DAYS + 1))
    assert due(path, now=end - timedelta(days=RENEW_WINDOW_DAYS - 1))
    assert not due(tmp_path / "missing.crt")  # nothing to compare a new one with
    assert not list(tmp_path.glob("*.new"))


async def test_a_running_node_renews_and_stores_its_certificate(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole way: the agent notices the end is near, asks, checks and stores."""
    from nox.node import protocol, renewal

    monkeypatch.setattr(protocol, "RENEW_WINDOW_DAYS", 40)  # a fresh certificate counts as due
    monkeypatch.setattr(renewal, "RENEW_WINDOW_DAYS", 40)
    node = await pair_node(pairing, tmp_path)
    home = NodeHome(tmp_path / "node-home")
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
        ),
        cert_pem=node.cert_pem,
        ca_pem=tls.ca_file.read_text(),
    )
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    agent = NodeAgent(
        home,
        NodeKeyStore(home.key_file, None),
        NodeGate(default_tools(), NodeManifest(), audit),
        audit,
        ping_interval_s=3600,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(agent.run(stop))
    try:
        for _ in range(250):
            if home.cert_file.read_text() != node.cert_pem:
                break
            await asyncio.sleep(0.02)
        renewed = x509.load_pem_x509_certificate(home.cert_file.read_bytes())
        assert f"{renewed.serial_number:x}" != node.serial
        assert any(e.action == "node.cert_renewed" and e.result == "ok" for e in audit.entries())
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_one_renewal_per_twelve_hours_whatever_the_connections(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.node import pairing as pairing_module

    monkeypatch.setattr(pairing_module, "RENEW_MIN_INTERVAL", timedelta(hours=12))
    node = await pair_node(pairing, tmp_path)  # pairing issued one a moment ago
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    listener.sessions[node.node_id].cert_not_after = datetime.now(UTC) + timedelta(days=2)
    answer = await _ask(ws, make_csr(node.key))
    assert answer["error"]["message"] == "refused"  # type: ignore[index]
    await ws.close()


async def test_a_certificate_not_valid_yet_or_without_a_current_one_is_refused(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.lan.tls import issue_client_certificate
    from nox.node import renewal

    node = await pair_node(pairing, tmp_path)
    fresh = issue_client_certificate(tmp_path / "tls", make_csr(node.key), node.node_id)

    def check(current: Path) -> None:
        accept_renewed(
            fresh.cert_pem,
            ca_file=tls.ca_file,
            current_file=current,
            key=node.key,
            node_id=node.node_id,
        )

    check(node.cert_file)  # as issued: fine
    with pytest.raises(RenewalRefusedError, match="unreadable"):
        check(tmp_path / "missing.crt")

    class Earlier(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime.now(UTC) - timedelta(hours=1)

    monkeypatch.setattr(renewal, "datetime", Earlier)
    with pytest.raises(RenewalRefusedError, match="not valid yet"):
        check(node.cert_file)


async def test_concurrent_renewals_issue_exactly_one_certificate(
    pairing: NodePairing,
    repo: object,
    nodes: NodeRepository,
    tmp_path: Path,
) -> None:
    """Several connections - and a second process with its own lock - ask at the same moment:
    one certificate, the old serial revoked once, every other request refused."""
    second_process = NodePairing(repo, nodes, tls_dir=tmp_path / "tls")  # type: ignore[arg-type]
    node = await pair_node(pairing, tmp_path)
    csr = make_csr(node.key)
    results = await asyncio.gather(
        *(
            (pairing if i % 2 else second_process).renew(node.node_id, node.serial, csr)
            for i in range(6)
        )
    )
    issued = [r for r in results if r is not None]
    assert len(issued) == 1
    certs = nodes._db.fetch_all(  # noqa: SLF001
        "SELECT serial, revoked_at FROM node_certs WHERE device_id = ?", (node.node_id,)
    )
    assert len(certs) == 2  # the paired one, now revoked, and exactly one new one
    old = nodes.get_cert(node.serial)
    assert old is not None and old.revoked
    new = nodes.get_cert(issued[0].serial)
    assert new is not None and not new.revoked
