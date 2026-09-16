"""A slow bus handler for a worker's inbound event must not stall that worker's requests: the
voice worker's heartbeats and `tts.finished` used to queue behind a whole LLM+TTS turn until the
core dropped the connection (2026-09-15)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from nox.core.events import Event
from nox.ipc.protocol import Kind
from nox.ipc.server import IpcHub
from nox.ipc.tokens import TokenStore
from tests.unit.ipc.conftest import RawClient, SimpleBus

RawFactory = Callable[..., Awaitable[RawClient]]


async def test_slow_event_handler_does_not_block_requests_on_the_same_connection(
    raw: RawFactory, tokens: TokenStore, bus: SimpleBus, hub: IpcHub
) -> None:
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_handler(event: Event) -> None:
        seen.append(event.name)
        await release.wait()  # simulates LLM + TTS inside the handler

    bus.subscribe("stt.ready", slow_handler)
    wt = tokens.issue_worker_token("worker:stt")
    w = await raw("worker", "worker:stt")
    assert (await w.auth(wt)).kind is Kind.RESPONSE
    assert (
        await w.request("worker.register", {"service": "stt", "services": ["stt"]})
    ).kind is Kind.RESPONSE

    await w.send(Kind.EVENT, "stt.ready", {"model": "small"})
    await asyncio.sleep(0.05)
    assert seen == ["stt.ready"]  # handler is running and blocked
    pong = await asyncio.wait_for(w.request("ipc.ping", {}), timeout=1.0)
    assert pong.kind is Kind.RESPONSE  # requests still flow while the handler hangs
    release.set()
    await asyncio.sleep(0.05)


async def test_events_from_one_client_are_delivered_in_order(
    raw: RawFactory, tokens: TokenStore, bus: SimpleBus, hub: IpcHub
) -> None:
    order: list[str] = []

    async def handler(event: Event) -> None:
        await asyncio.sleep(0.01)
        order.append(str(event.payload.get("model")))

    bus.subscribe("stt.ready", handler)
    wt = tokens.issue_worker_token("worker:stt")
    w = await raw("worker", "worker:stt")
    assert (await w.auth(wt)).kind is Kind.RESPONSE
    await w.request("worker.register", {"service": "stt", "services": ["stt"]})
    for i in range(5):
        await w.send(Kind.EVENT, "stt.ready", {"model": f"m{i}"})
    await asyncio.sleep(0.3)
    assert order == ["m0", "m1", "m2", "m3", "m4"]
