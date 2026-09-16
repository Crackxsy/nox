"""After the hub dropped the worker (core restart, stalled dispatch) the reconnected worker must
register again and report ready, otherwise the core keeps a dead client id (2026-09-15)."""

from __future__ import annotations

import asyncio

from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker import spy_factory


async def test_reregisters_and_reports_ready_after_a_reconnect() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=0.5
    )
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    lifecycle = [n for n, _ in client.requests if n != "worker.heartbeat"]
    assert lifecycle == ["worker.register", "worker.ready"]

    worker.on_connection_change(False)  # connection lost
    worker.on_connection_change(True)  # IpcClient reconnected with a fresh session
    await asyncio.sleep(0.1)
    lifecycle = [n for n, _ in client.requests if n != "worker.heartbeat"]
    assert lifecycle == ["worker.register", "worker.ready", "worker.register", "worker.ready"]

    worker.request_stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_connection_change_before_first_registration_is_ignored() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    worker.on_connection_change(True)
    await asyncio.sleep(0.05)
    assert client.requests == []
