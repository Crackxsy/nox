"""What one node cannot do to the listener or to another node: stall a TLS handshake, hold
the hello slots, flood the audit trail with repeated kills, or leave unfinished blobs and
connections behind."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import InvalidStatus

from nox.lan.tls import TlsMaterial
from nox.node import listener as listener_module
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.repo import NodeRepository
from nox.security.audit import SqliteAuditLog
from tests.unit.node.support import client_context, greet, open_node, pair_node, recv_until


async def _listener(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, **kwargs: Any
) -> NodeListener:
    server = NodeListener(
        pairing=pairing, nodes=nodes, tls=lambda: tls, host="127.0.0.1", port=0, **kwargs
    )
    await server.start()
    return server


# ---- M2: half-open and slow connections are bounded --------------------------------------------


async def test_a_stalled_tls_handshake_is_dropped(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener_module, "HANDSHAKE_TIMEOUT_S", 0.5)
    server = await _listener(pairing, nodes, tls)
    try:
        raw = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        raw.settimeout(5)
        started = time.monotonic()
        # Say nothing: no ClientHello. The server must give up on its own, long before 60 s.
        data = await asyncio.to_thread(raw.recv, 1024)
        assert data == b""
        assert time.monotonic() - started < 3.0
        raw.close()
    finally:
        await server.stop()


async def test_one_node_cannot_lock_out_another(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M2: a node (or anyone behind the same address) filling the waiting slots only
    displaces its own older attempts; another node still gets in."""
    monkeypatch.setattr(listener_module, "MAX_PREAUTH_TOTAL", 3)
    server = await _listener(pairing, nodes, tls)
    try:
        noisy = await pair_node(pairing, tmp_path)
        legit = await pair_node(pairing, tmp_path)
        held = [await open_node(server, client_context(tls, noisy)) for _ in range(6)]
        waiting = [d for d, _ in server._preauth.values()]
        assert waiting.count(noisy.node_id) == 2  # older attempts were displaced
        ws = await open_node(server, client_context(tls, legit))
        assert (await greet(ws))["result"]["node_id"] == legit.node_id
        await ws.close()
        for h in held:
            await h.close()
    finally:
        await server.stop()


async def test_slow_hellos_are_capped_in_total(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(listener_module, "MAX_PREAUTH_TOTAL", 1)
    server = await _listener(pairing, nodes, tls)
    try:
        a = await pair_node(pairing, tmp_path)
        b = await pair_node(pairing, tmp_path)
        held = await open_node(server, client_context(tls, a))
        with pytest.raises(InvalidStatus) as refused:
            await open_node(server, client_context(tls, b))
        assert refused.value.response.status_code == 503
        await held.close()
    finally:
        await server.stop()


# ---- M3: a node repeating security.kill cannot flood the audit trail ---------------------------


async def test_repeats_while_stopped_change_nothing_and_are_audited_once(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> None:
    audit = SqliteAuditLog(sqlite3.connect(":memory:", check_same_thread=False))
    kills: list[str] = []
    engaged = [False]

    async def on_kill(device_id: str, _reason: str) -> None:
        kills.append(device_id)
        engaged[0] = True

    server = await _listener(
        pairing, nodes, tls, audit=audit, on_security_kill=on_kill, kill_engaged=lambda: engaged[0]
    )
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)
        first = {"jsonrpc": "2.0", "id": 99, "method": "security.kill", "params": {}}
        await ws.send(json.dumps(first))
        assert (await recv_until(ws, lambda m: m.get("id") == 99))["result"]["status"] == "ok"
        for _ in range(100):
            if engaged[0]:
                break
            await asyncio.sleep(0.01)
        statuses = []
        for i in range(30):
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 100 + i, "method": "security.kill", "params": {}}
                )
            )
            answer = await recv_until(ws, lambda m, i=i: m.get("id") == 100 + i)
            statuses.append(answer["result"]["status"])
        assert set(statuses) == {"already_engaged"}
        assert kills == [node.node_id]
        audited = [e for e in audit.entries(limit=500) if e.action == "node.security_kill"]
        assert len(audited) == 1  # the stop; repeats within the interval leave no trace
        await ws.close()
    finally:
        await server.stop()


async def test_a_kill_after_a_resume_always_takes_effect(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Review follow-up: kill -> resume -> an immediate new kill must stop Nox again - a safety
    signal is never throttled away."""
    kills: list[str] = []
    engaged = [False]

    async def on_kill(device_id: str, _reason: str) -> None:
        kills.append(device_id)
        engaged[0] = True

    server = await _listener(
        pairing, nodes, tls, on_security_kill=on_kill, kill_engaged=lambda: engaged[0]
    )
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)
        for i in range(2):
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 300 + i, "method": "security.kill", "params": {}}
                )
            )
            answer = await recv_until(ws, lambda m, i=i: m.get("id") == 300 + i)
            assert answer["result"]["status"] == "ok"
            for _ in range(100):
                if engaged[0]:
                    break
                await asyncio.sleep(0.01)
            engaged[0] = False  # the owner pressed "Fortsetzen"
        assert kills == [node.node_id, node.node_id]
        await ws.close()
    finally:
        await server.stop()


# ---- N8: unconsumed blobs do not linger ---------------------------------------------------------


async def test_an_unfinished_blob_is_dropped_after_its_time(
    pairing: NodePairing,
    nodes: NodeRepository,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    server = await _listener(pairing, nodes, tls)
    try:
        node = await pair_node(pairing, tmp_path)
        ws = await open_node(server, client_context(tls, node))
        await greet(ws)

        def blob(call_id: str, msg_id: int) -> str:
            params = {
                "call_id": call_id,
                "seq": 0,
                "last": False,
                "data": base64.b64encode(b"x" * 100).decode(),
            }
            return json.dumps(
                {"jsonrpc": "2.0", "id": msg_id, "method": "nox/blob", "params": params}
            )

        await ws.send(blob("old", 1))
        await recv_until(ws, lambda m: m.get("id") == 1)
        session = server.sessions[node.node_id]
        assert session.blob_bytes == 100
        monkeypatch.setattr(listener_module, "BLOB_TTL_S", 0.0)
        await asyncio.sleep(0.01)
        await ws.send(blob("new", 2))
        await recv_until(ws, lambda m: m.get("id") == 2)
        assert "old" not in session.blobs and session.blob_bytes == 100
        await ws.close()
    finally:
        await server.stop()


async def test_identified_connections_are_held_weakly(
    pairing: NodePairing, nodes: NodeRepository, tls: TlsMaterial
) -> None:
    """A connection whose upgrade fails after the certificate check leaves no entry behind."""
    import gc
    import weakref

    server = NodeListener(pairing=pairing, nodes=nodes, tls=lambda: tls, host="127.0.0.1", port=0)
    assert isinstance(server._identified, weakref.WeakKeyDictionary)

    class Conn:
        pass

    conn = Conn()
    server._identified[conn] = ("device", "cert")  # type: ignore[index]
    del conn
    gc.collect()
    assert len(server._identified) == 0
