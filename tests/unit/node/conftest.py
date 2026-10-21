"""Fixtures for the node tests: a migrated database, a fresh authority per test (in `tmp_path`,
never the real Nox folder), node pairing, and a scripted node that speaks raw JSON-RPC - so a
test can send exactly what a malicious node would."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.asyncio.client import ClientConnection, connect

from nox.data.db import Database
from nox.lan.tls import TlsMaterial, ensure_tls
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.protocol import NODE_PATH, NodePairRequest
from nox.node.repo import NodeRepository
from nox.remote.repo import RemoteRepository
from tests.unit.ipc.conftest import SimpleBus

NAMES = ("localhost", "wohnzimmer-pi.local")
ADDRESSES = ("127.0.0.1", "192.168.1.20")
MANIFEST = "0" * 64


def make_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def make_csr(key: Any, *, cn: str = "Laptop", extra: bool = False) -> str:
    builder = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    )
    if extra:  # what a greedy node might ask for: a CA, a server name
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        ).add_extension(x509.SubjectAlternativeName([x509.DNSName("evil.example")]), critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@dataclass
class PairedNode:
    node_id: str
    key: ec.EllipticCurvePrivateKey
    cert_pem: str
    serial: str
    cert_file: Path
    key_file: Path


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


async def pair_node(pairing: NodePairing, tmp_path: Path, name: str = "Laptop") -> PairedNode:
    key = make_key()
    start = await pairing.start(name=name)
    redeemed = await pairing.redeem(
        NodePairRequest(
            code=start.code,
            csr=make_csr(key),
            name=name,
            os="linux",
            arch="x86_64",
            version="0.7.0",
        )
    )
    assert redeemed.result.ok and redeemed.issued is not None, redeemed.result
    folder = tmp_path / f"node-{redeemed.result.device_id[:8]}"
    folder.mkdir()
    (folder / "node.crt").write_text(redeemed.issued.cert_pem)
    (folder / "node.key").write_bytes(key_pem(key))
    return PairedNode(
        node_id=redeemed.result.device_id,
        key=key,
        cert_pem=redeemed.issued.cert_pem,
        serial=redeemed.issued.serial,
        cert_file=folder / "node.crt",
        key_file=folder / "node.key",
    )


@pytest.fixture
async def listener(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial
) -> AsyncIterator[NodeListener]:
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
    )
    server.kills = kills  # type: ignore[attr-defined]
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def client_context(
    tls: TlsMaterial, node: PairedNode | None = None, *, max_tls12: bool = False
) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    if node is not None:
        context.load_cert_chain(str(node.cert_file), str(node.key_file))
    if max_tls12:
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


async def open_node(
    listener: NodeListener,
    context: ssl.SSLContext,
    *,
    origin: str | None = None,
    path: str = NODE_PATH,
) -> ClientConnection:
    return await connect(
        f"wss://127.0.0.1:{listener.port}{path}",
        ssl=context,
        server_hostname="localhost",
        origin=origin,  # type: ignore[arg-type]
        open_timeout=5,
        ping_interval=None,
        compression=None,
        max_size=2 * 1024 * 1024,
    )


def hello(node_protocol: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "node_protocol": node_protocol,
            "node_version": "0.7.0",
            "os": "linux",
            "arch": "x86_64",
            "manifest_hash": MANIFEST,
        },
    }


async def recv_json(ws: ClientConnection, timeout: float = 5.0) -> dict[str, Any]:
    message: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), timeout))
    return message


async def recv_until(ws: ClientConnection, predicate: Any, timeout: float = 5.0) -> dict[str, Any]:
    async def loop() -> dict[str, Any]:
        while True:
            message = await recv_json(ws, timeout)
            if predicate(message):
                return message

    return await asyncio.wait_for(loop(), timeout)


async def greet(ws: ClientConnection, node_protocol: int = 1) -> dict[str, Any]:
    await ws.send(json.dumps(hello(node_protocol)))
    return await recv_until(ws, lambda m: m.get("id") == 1)
