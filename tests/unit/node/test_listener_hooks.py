"""`NodeListener.on_connected` and `on_disconnected`: lists of hooks that run in the order they were
added, over a real connection - so an extension joins them without taking another one's place."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.lan.tls import TlsMaterial
from nox.node.hub.listener import NodeListener, NodeSession
from nox.node.hub.pairing import NodePairing
from tests.unit.fakes import wait_until
from tests.unit.node.support import client_context, greet, open_node, pair_node

pytestmark = pytest.mark.asyncio


async def test_every_hook_runs_for_a_connecting_node_in_the_order_it_was_added(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    calls: list[str] = []
    ready_when_called: list[bool] = []

    def hook(label: str) -> Any:
        async def run(session: NodeSession) -> None:
            calls.append(f"{label}:{session.device_id[:4]}")
            ready_when_called.append(session.ready.is_set())

        return run

    listener.on_connected.append(hook("first"))
    listener.on_connected.append(hook("second"))
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await wait_until(lambda: len(calls) == 2, what="both connect hooks to run")
    assert calls == [f"first:{node.node_id[:4]}", f"second:{node.node_id[:4]}"]
    assert ready_when_called == [True, True]  # a hook sees a node that is ready for requests
    await ws.close()


async def test_a_connect_hook_that_raises_ends_the_connection_and_skips_the_later_ones(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def refuses(_session: NodeSession) -> None:
        calls.append("refuses")
        raise RuntimeError("not this node")

    async def never(_session: NodeSession) -> None:
        calls.append("never")

    listener.on_connected.append(refuses)
    listener.on_connected.append(never)
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await wait_until(lambda: node.node_id not in listener.sessions, what="the node to go")
    assert calls == ["refuses"]
    await ws.close()


async def test_every_disconnect_hook_runs_after_the_node_is_gone_in_the_order_added(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    calls: list[tuple[str, bool]] = []
    node = await pair_node(pairing, tmp_path)

    def hook(label: str) -> Any:
        async def run(session: NodeSession) -> None:
            calls.append((label, session.device_id in listener.sessions))

        return run

    listener.on_disconnected.append(hook("first"))
    listener.on_disconnected.append(hook("second"))
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await wait_until(lambda: node.node_id in listener.sessions, what="the node to show up")
    await ws.close()
    await wait_until(lambda: len(calls) == 2, what="both disconnect hooks to run")
    assert calls == [("first", False), ("second", False)]  # the session is already removed


async def test_a_disconnect_hook_that_raises_skips_the_later_ones_and_nothing_else(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def broken(_session: NodeSession) -> None:
        calls.append("broken")
        raise RuntimeError("cannot forget this node")

    async def after(_session: NodeSession) -> None:
        calls.append("after")

    listener.on_disconnected.append(broken)
    listener.on_disconnected.append(after)
    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    await wait_until(lambda: node.node_id in listener.sessions, what="the node to show up")
    await ws.close()
    await wait_until(lambda: bool(calls), what="the disconnect hooks to run")
    assert calls == ["broken"] and node.node_id not in listener.sessions
