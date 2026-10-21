"""Node pairing on the core: a one-time code for a certificate, and `identify` - the question the
node listener asks after every handshake."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509

from nox.lan.tls import TlsMaterial
from nox.node.pairing import NodePairing
from nox.node.protocol import NodePairRequest
from nox.node.repo import NodeRepository
from nox.remote.repo import RemoteRepository
from tests.unit.node.conftest import make_csr, make_key, pair_node


def _request(code: str, csr: str | None = None) -> NodePairRequest:
    return NodePairRequest(
        code=code,
        csr=csr or make_csr(make_key()),
        name="",
        os="windows",
        arch="AMD64",
        version="0.7.0",
    )


def _cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


async def test_a_code_yields_a_node_with_one_certificate(
    pairing: NodePairing, repo: RemoteRepository, nodes: NodeRepository
) -> None:
    start = await pairing.start(name="Arbeits-PC")
    redeemed = await pairing.redeem(_request(start.code))
    assert redeemed.result.ok and redeemed.issued is not None
    device = repo.get_device(redeemed.result.device_id)
    assert device is not None and device.role == "node" and device.channel == "node"
    node = nodes.get_node(device.id)
    assert node is not None and node.os == "windows"
    row = nodes.get_cert(redeemed.issued.serial)
    assert row is not None and row.device_id == device.id and not row.revoked


async def test_a_malformed_request_costs_no_code(pairing: NodePairing) -> None:
    start = await pairing.start()
    garbage = "-----BEGIN CERTIFICATE REQUEST-----\n" + "A" * 80
    bad = await pairing.redeem(_request(start.code, csr=garbage))
    assert bad.result.reason == "bad_csr"
    good = await pairing.redeem(_request(start.code))
    assert good.result.ok


async def test_a_code_works_once(pairing: NodePairing) -> None:
    start = await pairing.start()
    assert (await pairing.redeem(_request(start.code))).result.ok
    again = await pairing.redeem(_request(start.code))
    assert not again.result.ok and again.result.reason == "used"
    assert again.issued is None


async def test_an_unknown_code_yields_nothing(pairing: NodePairing) -> None:
    redeemed = await pairing.redeem(_request("ABCDEFGH"))
    assert not redeemed.result.ok and redeemed.issued is None


async def test_the_same_key_cannot_pair_twice(pairing: NodePairing) -> None:
    csr = make_csr(make_key())
    first = await pairing.redeem(_request((await pairing.start()).code, csr))
    second = await pairing.redeem(_request((await pairing.start()).code, csr))
    assert first.result.ok and second.result.reason == "already_paired"


async def test_a_failed_signature_leaves_no_half_paired_node(
    pairing: NodePairing, repo: RemoteRepository, tmp_path: Path
) -> None:
    (tmp_path / "tls" / "ca.key").unlink()  # the authority cannot sign any more
    redeemed = await pairing.redeem(_request((await pairing.start()).code))
    assert redeemed.result.reason == "issue_failed"
    assert [d for d in repo.list_devices(include_revoked=False) if d.channel == "node"] == []


async def test_identify_accepts_the_issued_certificate(
    pairing: NodePairing, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    device = pairing.identify(_cert(node.cert_pem))
    assert device is not None and device.id == node.node_id


async def test_identify_refuses_after_revocation(pairing: NodePairing, tmp_path: Path) -> None:
    node = await pair_node(pairing, tmp_path)
    closed: list[tuple[str, str]] = []

    async def on_revoked(device_id: str, reason: str) -> None:
        closed.append((device_id, reason))

    pairing.on_revoked(on_revoked)
    assert await pairing.revoke(node.node_id, reason="lost")
    assert closed == [(node.node_id, "lost")]
    assert pairing.identify(_cert(node.cert_pem)) is None
    assert not await pairing.revoke(node.node_id)  # twice is a no-op


async def test_revocation_denylists_every_certificate_of_the_node(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    await pairing.revoke(node.node_id)
    assert node.serial in nodes.revoked_serials()


async def test_identify_refuses_a_certificate_whose_serial_was_never_issued(
    pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    from nox.lan.tls import issue_client_certificate

    node = await pair_node(pairing, tmp_path)
    stray = issue_client_certificate(tmp_path / "tls", make_csr(node.key), node.node_id)
    # Same key, same node - but a serial the core never recorded (a copy of the CA key at work).
    assert pairing.identify(_cert(stray.cert_pem)) is None


async def test_identify_refuses_a_recorded_serial_for_another_node(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    a = await pair_node(pairing, tmp_path, name="Laptop")
    b = await pair_node(pairing, tmp_path, name="Arbeits-PC")
    # b's certificate row rewritten to point at a: the device id in the certificate disagrees.
    nodes._db.execute("UPDATE node_certs SET device_id = ? WHERE serial = ?", (a.node_id, b.serial))
    assert pairing.identify(_cert(b.cert_pem)) is None


async def test_identify_refuses_when_the_recorded_key_differs(
    pairing: NodePairing, nodes: NodeRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    nodes._db.execute(
        "UPDATE node_certs SET spki_sha256 = ? WHERE serial = ?", ("0" * 64, node.serial)
    )
    assert pairing.identify(_cert(node.cert_pem)) is None


async def test_identify_refuses_when_the_pairing_key_differs(
    pairing: NodePairing, repo: RemoteRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    repo._db.execute("UPDATE paired_devices SET public_key = 'x' WHERE id = ?", (node.node_id,))
    assert pairing.identify(_cert(node.cert_pem)) is None


async def test_the_node_limit_is_enforced(
    repo: RemoteRepository, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> None:
    small = NodePairing(repo, nodes, tls_dir=tmp_path / "tls", max_nodes=1)
    assert (await small.redeem(_request((await small.start()).code))).result.ok
    second = await small.redeem(_request((await small.start()).code))
    assert second.result.reason == "max_devices"


async def test_a_lan_device_is_never_identified_as_a_node(
    pairing: NodePairing, repo: RemoteRepository, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    repo._db.execute("UPDATE paired_devices SET role = 'device' WHERE id = ?", (node.node_id,))
    assert pairing.identify(_cert(node.cert_pem)) is None


@pytest.mark.parametrize("groups", [{"system": True, "shell": False}])
def test_effective_groups_need_the_node_and_the_core(
    nodes: NodeRepository, repo: RemoteRepository, groups: dict[str, bool]
) -> None:
    now = datetime.now(UTC)
    device = repo.create_device(
        name="Laptop",
        channel="node",
        role="node",
        sender_id="k",
        pairing_code_hash="",
        paired_at=now,
    )
    nodes.set_reported_groups(device.id, {**groups, "notify": True}, when=now)
    assert nodes.effective_groups(device.id) == {"system", "notify"}
    nodes.narrow(device.id, "notify", True, when=now)
    assert nodes.effective_groups(device.id) == {"system"}
    # The core cannot widen: un-narrowing a group the node never enabled gives nothing.
    nodes.narrow(device.id, "shell", False, when=now)
    assert "shell" not in nodes.effective_groups(device.id)
    # A new report drops what the node no longer enables.
    nodes.set_reported_groups(device.id, {"notify": True}, when=now)
    assert nodes.effective_groups(device.id) == set()
