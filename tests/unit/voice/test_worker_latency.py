"""`voice.latency` on the worker: the pipeline's turn timings as the answer to a request."""

from __future__ import annotations

import pytest

from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker_speech import Built, start


@pytest.fixture
def client() -> FakeIpcClient:
    return FakeIpcClient()


async def test_the_latency_request_returns_the_pipelines_summary(client: FakeIpcClient) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=60.0
    )
    task = await start(worker)

    reply = await client.deliver_request("voice.latency", {})

    assert reply["ok"] is True
    assert reply["turns"] == 0 and reply["window"] == 50
    assert reply["total_ms"] is None and reply["last"] is None

    worker.request_stop()
    await task
