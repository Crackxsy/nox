"""`voice.latency`: the dashboard asks the voice worker how long recent voice turns took."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nox.core.boot.workers import WorkerSupervisor
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.handlers.core import CoreHandlers
from nox.ipc.protocol import Envelope, Kind, Source


def supervisor() -> WorkerSupervisor:
    return WorkerSupervisor(
        job=cast("Any", None),
        command=["worker"],
        cwd=Path("."),
        hub_url=lambda: "ws://127.0.0.1:1/ws",
        data_dir=Path("."),
    )


class Hub:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def request(
        self, client_id: str, name: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        self.sent.append((client_id, name, payload))
        return self.reply


class Core:
    def __init__(self, workers: WorkerSupervisor, hub: Hub) -> None:
        self.workers = workers
        self.hub = hub


def context() -> RequestContext:
    env = Envelope(
        kind=Kind.REQUEST,
        name="voice.latency",
        src=Source(role="dashboard", id="dashboard:1"),
        payload={},
    )
    return RequestContext(client_id="dashboard:1", role="dashboard", request=env)


async def test_voice_latency_asks_the_voice_worker() -> None:
    workers = supervisor()
    workers.attach("voice", "worker:voice").registered.set()
    hub = Hub({"ok": True, "turns": 3, "window": 50})
    core = Core(workers, hub)
    handlers = CoreHandlers(cast("Any", core))

    reply = await handlers.voice_latency(context(), EmptyPayload())

    assert reply == {"ok": True, "turns": 3, "window": 50}
    assert hub.sent == [("worker:voice", "voice.latency", {})]


async def test_voice_latency_says_so_when_there_is_no_voice_worker() -> None:
    core = Core(supervisor(), Hub({}))
    handlers = CoreHandlers(cast("Any", core))

    reply = await handlers.voice_latency(context(), EmptyPayload())

    assert reply == {"ok": False, "reason": "voice worker unavailable"}


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("dashboard", True), ("shell", True), ("device", False), ("pet", False), ("plugin", False)],
)
def test_voice_latency_is_for_the_local_dashboards_only(role: str, allowed: bool) -> None:
    core = SimpleNamespace(registry=RequestRegistry())
    CoreHandlers(cast("Any", core)).register()

    assert core.registry.is_allowed("voice.latency", role) is allowed
