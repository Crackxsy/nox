"""The worker retries the hub handshake while the core is still booting (2026-09-15: a single 5 s
handshake timeout killed the voice worker for the whole session)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker import spy_factory


class FlakyClient(FakeIpcClient):
    def __init__(self, failures: list[IpcError]) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0

    async def connect(self) -> Any:
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return await super().connect()


def _unavailable() -> IpcError:
    return IpcError(
        ERR_UNAVAILABLE, "cannot connect: timed out during opening handshake", retryable=True
    )


async def test_retries_retryable_connect_errors_until_the_hub_answers() -> None:
    client = FlakyClient([_unavailable(), _unavailable()])
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=spy_factory,
        heartbeat_s=0.02,
        connect_backoff_s=0.01,
        connect_deadline_s=5.0,
    )
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.2)
    assert client.attempts == 3 and client.connected
    assert [n for n, _ in client.requests][:1] == ["worker.register"]
    worker.request_stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_gives_up_at_the_deadline() -> None:
    client = FlakyClient([_unavailable() for _ in range(50)])
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=spy_factory,
        connect_backoff_s=0.02,
        connect_deadline_s=0.05,
    )
    with pytest.raises(IpcError):
        await asyncio.wait_for(worker.run(), timeout=2.0)
    assert 1 <= client.attempts <= 4


async def test_non_retryable_error_surfaces_immediately() -> None:
    client = FlakyClient([IpcError(ERR_PERMISSION, "bad token", retryable=False)])
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, connect_backoff_s=0.01
    )
    with pytest.raises(IpcError):
        await asyncio.wait_for(worker.run(), timeout=2.0)
    assert client.attempts == 1
