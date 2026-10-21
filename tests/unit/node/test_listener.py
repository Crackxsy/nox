"""The node listener over real TLS on loopback. Every control has a test that goes red without it:
the client certificate, the authority, TLS 1.3, the denylist, revoked pairings, the paired key,
no browser Origin, the first-message rule, the role `node` allow-list and its cut-off, frame
and blob limits, and revocation closing a live connection in under a second."""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from websockets.exceptions import ConnectionClosed, InvalidMessage, InvalidStatus

from nox.lan.tls import TlsMaterial, ensure_tls, issue_client_certificate
from nox.node import listener as listener_module
from nox.node.listener import MAX_PENDING_BLOBS, MAX_VIOLATIONS, NodeListener
from nox.node.pairing import NodePairing
from nox.node.protocol import MAX_RESULT_BYTES
from nox.node.repo import NodeRepository
from tests.unit.node.conftest import (
    client_context,
    greet,
    hello,
    key_pem,
    make_csr,
    make_key,
    open_node,
    pair_node,
    recv_until,
)

pytestmark = pytest.mark.asyncio

#: How a refused TLS 1.3 client certificate looks from the client: the alert arrives after the
#: client thinks the handshake is done, so it may surface as a failed HTTP upgrade - but never as
#: an HTTP status, which would mean the request got past TLS.
TLS_REFUSED = (
    ssl.SSLError,
    ConnectionError,
    OSError,
    ConnectionClosed,
    InvalidMessage,
    TimeoutError,
)


async def _connected(listener: NodeListener, node_id: str) -> None:
    for _ in range(100):
        if node_id in listener.sessions:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the node never showed up as a session")


# ---- the happy path ----------------------------------------------------------------------------


async def test_a_paired_node_connects_and_the_core_lists_its_tools(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    answer = await greet(ws)
    assert answer["result"]["compatible"] is True
    assert answer["result"]["node_id"] == node.node_id
    assert answer["result"]["core_id"] == tls.fingerprint.replace(":", "").lower()
    await _connected(listener, node.node_id)
    session = listener.sessions[node.node_id]

    async def serve_tools() -> None:
        request = await recv_until(ws, lambda m: m.get("method") == "tools/list")
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"tools": [{"name": "system.info"}]},
                }
            )
        )

    serving = asyncio.create_task(serve_tools())
    tools = await session.client.list_tools()
    await serving
    assert [t.name for t in tools] == ["system.info"]
    await ws.close()


async def test_an_outdated_node_is_accepted_but_marked_incompatible(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    answer = await greet(ws, node_protocol=99)
    assert answer["result"]["compatible"] is False
    await _connected(listener, node.node_id)
    assert listener.sessions[node.node_id].compatible is False
    await ws.close()


# ---- who may connect at all --------------------------------------------------------------------


async def test_no_client_certificate_never_completes_the_handshake(
    listener: NodeListener, tls: TlsMaterial
) -> None:
    with pytest.raises(TLS_REFUSED):
        ws = await open_node(listener, client_context(tls))
        await greet(ws)


async def test_a_certificate_of_another_authority_is_refused(
    listener: NodeListener, tls: TlsMaterial, tmp_path: Path
) -> None:
    foreign = ensure_tls(tmp_path / "foreign", names=("localhost",), addresses=("127.0.0.1",))
    key = make_key()
    node_id = "f" * 32
    issued = issue_client_certificate(tmp_path / "foreign", make_csr(key), node_id)
    (tmp_path / "f.crt").write_text(issued.cert_pem)
    (tmp_path / "f.key").write_bytes(key_pem(key))
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    context.load_cert_chain(str(tmp_path / "f.crt"), str(tmp_path / "f.key"))
    assert foreign.fingerprint != tls.fingerprint
    with pytest.raises(TLS_REFUSED):
        ws = await open_node(listener, context)
        await greet(ws)


async def test_tls_1_2_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    with pytest.raises(TLS_REFUSED):
        await open_node(listener, client_context(tls, node, max_tls12=True))


async def test_a_browser_origin_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node), origin="https://192.168.1.20:47843")
    assert refused.value.response.status_code == 403


async def test_only_the_node_path_exists(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node), path="/ws")
    assert refused.value.response.status_code == 404


async def test_a_denylisted_serial_is_refused(
    listener: NodeListener,
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
) -> None:
    node = await pair_node(pairing, tmp_path)
    nodes.revoke_certs(node.node_id, reason="test", when=datetime.now(UTC))
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node))
    assert refused.value.response.status_code == 403


async def test_a_revoked_pairing_is_refused_even_with_a_clean_serial(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    await pairing.pairing.revoke(node.node_id)  # the pairing row only, not the certificates
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node))
    assert refused.value.response.status_code == 403


async def test_a_second_certificate_for_the_same_node_but_another_key_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    # Signed by the right authority for the right node id - but never issued through pairing, so
    # neither its serial nor its key is on record.
    other = make_key()
    issued = issue_client_certificate(tmp_path / "tls", make_csr(other), node.node_id)
    (tmp_path / "o.crt").write_text(issued.cert_pem)
    (tmp_path / "o.key").write_bytes(key_pem(other))
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    context.load_cert_chain(str(tmp_path / "o.crt"), str(tmp_path / "o.key"))
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, context)
    assert refused.value.response.status_code == 403


async def test_the_servers_own_certificate_is_no_node_certificate(
    listener: NodeListener, tls: TlsMaterial
) -> None:
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    context.load_cert_chain(str(tls.cert_file), str(tls.key_file))
    with pytest.raises(TLS_REFUSED):
        ws = await open_node(listener, context)
        await greet(ws)


async def test_a_certificate_without_client_auth_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Hand-made with the authority's key: right node URI, right CN, but EKU serverAuth."""
    node = await pair_node(pairing, tmp_path)
    ca_key = serialization.load_pem_private_key((tmp_path / "tls" / "ca.key").read_bytes(), None)
    ca = x509.load_pem_x509_certificate(tls.ca_file.read_bytes())
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)
    now = datetime.now(UTC)
    from datetime import timedelta

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"node:{node.node_id}")]))
        .issuer_name(ca.subject)
        .public_key(node.key.public_key())
        .serial_number(int(node.serial, 16))
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"urn:nox:node:{node.node_id}")]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    (tmp_path / "s.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.create_default_context(cafile=str(tls.ca_file))
    context.load_cert_chain(str(tmp_path / "s.crt"), str(node.key_file))
    with pytest.raises(TLS_REFUSED):
        ws = await open_node(listener, context)
        await greet(ws)


# ---- the first message ---------------------------------------------------------------------------


async def test_the_first_message_must_be_initialize(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "nox/ping", "params": {}}))
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), 5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
    assert node.node_id not in listener.sessions


async def test_a_silent_node_is_dropped(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener_module, "HELLO_TIMEOUT_S", 0.3)
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)


async def test_a_hello_with_unknown_fields_is_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"node_protocol": 1, "role": "dashboard"},
    }
    await ws.send(json.dumps(message))
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(ws.recv(), 5)


# ---- the role `node` allow-list ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "chat.send",
        "memory.search",
        "security.permission.reply",
        "security.resume",
        "tools/call",  # a node cannot call tools - not the core's, not another node's
        "tools/list",
        "initialize",  # only as the first message
        "config.get",
        "nox/kill",  # core -> node only
    ],
)
async def test_a_node_may_not_send(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path, method: str
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 7, "method": method, "params": {}}))
    answer = await recv_until(ws, lambda m: m.get("id") == 7)
    assert answer["error"]["code"] == -32601
    assert listener.sessions[node.node_id].violations == 1
    assert listener.kills == []  # type: ignore[attr-defined]
    await ws.close()


async def test_repeated_violations_close_the_connection(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    for i in range(MAX_VIOLATIONS):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "chat.send", "params": {"i": i}}))
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1008
    assert node.node_id not in listener.sessions


async def test_malformed_params_count_as_a_violation(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "security.kill", "params": {"x": 1}})
    )
    answer = await recv_until(ws, lambda m: m.get("id") == 3)
    assert answer["error"]["code"] == -32602
    await asyncio.sleep(0.05)
    assert listener.kills == []  # type: ignore[attr-defined]
    await ws.close()


async def test_a_local_stop_on_the_node_reaches_the_core(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "method": "security.kill", "params": {"reason": "tray"}})
    )
    for _ in range(100):
        if listener.kills:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.02)
    assert listener.kills == [(node.node_id, "tray")]  # type: ignore[attr-defined]
    await ws.close()


async def test_ping_is_answered_with_pong(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "method": "nox/ping", "params": {"seq": 4, "ts": 1}})
    )
    pong = await recv_until(ws, lambda m: m.get("method") == "nox/pong")
    assert pong["params"]["seq"] == 4
    await ws.close()


async def test_a_binary_frame_ends_the_connection(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    await ws.send(b"\x00\x01")
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)


async def test_a_frame_over_one_mebibyte_ends_the_connection(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    padding = "x" * (1024 * 1024 + 16)
    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "nox/pong", "params": {"p": padding}}))
    with pytest.raises(ConnectionClosed) as closed:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 1009


# ---- blobs -------------------------------------------------------------------------------------


def _blob(call_id: str, seq: int, data: bytes, *, last: bool, msg_id: int | None = None) -> str:
    message: dict[str, object] = {
        "jsonrpc": "2.0",
        "method": "nox/blob",
        "params": {
            "call_id": call_id,
            "seq": seq,
            "last": last,
            "data": base64.b64encode(data).decode(),
        },
    }
    if msg_id is not None:
        message["id"] = msg_id
    return json.dumps(message)


async def test_blob_chunks_are_joined_in_order_and_bounded(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await ws.send(_blob("c1", 0, b"ab", last=False))
    await ws.send(_blob("c1", 1, b"cd", last=True, msg_id=11))
    await recv_until(ws, lambda m: m.get("id") == 11)
    session = listener.sessions[node.node_id]
    assert session.take_blob("c1") == b"abcd"
    assert session.take_blob("c1") is None
    # Out of order: refused.
    await ws.send(_blob("c2", 1, b"zz", last=True, msg_id=12))
    answer = await recv_until(ws, lambda m: m.get("id") == 12)
    assert answer["error"]["code"] == -32602
    await ws.close()


async def test_blobs_beyond_eight_mebibytes_are_refused(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    session = listener.sessions[node.node_id]
    session.blob_bytes = MAX_RESULT_BYTES - 1  # as if 8 MiB were already held
    await ws.send(_blob("big", 0, b"xy", last=True, msg_id=21))
    answer = await recv_until(ws, lambda m: m.get("id") == 21)
    assert answer["error"]["code"] == -32602
    assert session.take_blob("big") is None
    await ws.close()


# ---- revocation --------------------------------------------------------------------------------


async def test_revoking_closes_the_live_connection_in_under_a_second(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await _connected(listener, node.node_id)
    started = time.monotonic()
    assert await pairing.revoke(node.node_id, reason="lost laptop")
    seen_revoked = False
    with pytest.raises(ConnectionClosed):
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 2))
            seen_revoked = seen_revoked or message.get("method") == "nox/revoked"
    elapsed = time.monotonic() - started
    assert seen_revoked
    assert elapsed < 1.0, elapsed
    assert node.node_id not in listener.sessions
    # ... and the certificate never works again.
    with pytest.raises(InvalidStatus):
        await open_node(listener, client_context(tls, node))


async def test_a_reconnect_replaces_the_old_session(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    first = await open_node(listener, client_context(tls, node))
    await greet(first)
    await _connected(listener, node.node_id)
    old = listener.sessions[node.node_id]
    second = await open_node(listener, client_context(tls, node))
    await greet(second)
    for _ in range(100):
        if listener.sessions.get(node.node_id) is not old:
            break
        await asyncio.sleep(0.02)
    assert listener.sessions[node.node_id] is not old
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(first.recv(), 2)
    await second.close()


# ---- found in the self-review --------------------------------------------------------------------


async def test_a_public_peer_is_refused(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = await pair_node(pairing, tmp_path)
    monkeypatch.setattr(listener_module, "permitted_address", lambda _a: False)
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node))
    assert refused.value.response.status_code == 403


async def test_a_node_cut_off_for_violations_must_wait(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    for _ in range(MAX_VIOLATIONS):
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "chat.send", "params": {}}))
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    with pytest.raises(InvalidStatus) as refused:
        await open_node(listener, client_context(tls, node))
    assert refused.value.response.status_code == 429


async def test_a_node_revoked_while_saying_hello_gets_no_session(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await pairing.revoke(node.node_id)  # after the upgrade, before `initialize`
    await ws.send(json.dumps(hello()))
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert node.node_id not in listener.sessions


async def test_pending_blobs_are_bounded_in_number(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    for i in range(MAX_PENDING_BLOBS):
        await ws.send(_blob(f"b{i}", 0, b"", last=False))
    await ws.send(_blob("one-too-many", 0, b"", last=False, msg_id=31))
    answer = await recv_until(ws, lambda m: m.get("id") == 31)
    assert answer["error"]["code"] == -32602
    assert len(listener.sessions[node.node_id].blobs) == MAX_PENDING_BLOBS
    await ws.close()


async def test_a_revocation_during_the_replacement_window_closes_the_new_session(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Closing the old session is an await between identify and registration. A revocation that
    lands there must still close the new session at once, and it never gets another call."""
    node = await pair_node(pairing, tmp_path)
    first = await open_node(listener, client_context(tls, node))
    await greet(first)
    await _connected(listener, node.node_id)
    in_window = asyncio.Event()
    real_close = listener._close

    async def slow_close(session: Any, code: int, reason: str) -> None:
        if reason == "replaced by a new connection":
            in_window.set()
            await asyncio.sleep(0.3)  # the replacement window, stretched
        await real_close(session, code, reason)

    listener._close = slow_close  # type: ignore[method-assign]
    second = await open_node(listener, client_context(tls, node))
    await second.send(json.dumps(hello()))
    await asyncio.wait_for(in_window.wait(), 5)
    started = time.monotonic()
    assert await pairing.revoke(node.node_id, reason="lost laptop") is not None
    with pytest.raises(ConnectionClosed):
        while True:
            message = json.loads(await asyncio.wait_for(second.recv(), 2))
            assert message.get("method") not in ("tools/call", "tools/list")
    assert time.monotonic() - started < 1.0
    assert node.node_id not in listener.sessions
    await asyncio.sleep(0.2)
    assert node.node_id not in listener.sessions
