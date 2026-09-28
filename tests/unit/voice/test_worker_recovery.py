"""The voice worker says why it failed, and ends itself when the core is gone for good."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nox.core.config import VoiceConfig
from nox.ipc.errors import ERR_AUTH_DENIED, IpcError
from nox.voice.base import VoicePipeline
from nox.worker.hub_loss import EXIT_HUB_LOST
from nox.worker.main import VoiceWorker, WorkerStartupError
from tests.unit.voice.conftest import FakeIpcClient, settle
from tests.unit.voice.test_worker import spy_factory


async def missing_model(_cfg: VoiceConfig) -> tuple[Any, Any]:
    raise FileNotFoundError("voice model missing: /home/someone/models/piper/de_DE.onnx")


async def test_a_model_that_cannot_load_is_reported_before_the_worker_exits() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=spy_factory,
        component_loader=missing_model,
    )

    with pytest.raises(WorkerStartupError, match="voice model missing"):
        await worker.run()

    failed = [payload for name, payload in client.requests if name == "worker.failed"]
    reason = "FileNotFoundError: voice model missing: /home/someone/models/piper/de_DE.onnx"
    assert failed == [{"service": "voice", "reason": reason}]
    assert "worker.ready" not in [name for name, _ in client.requests]
    assert client.closed


async def test_a_failure_report_that_cannot_be_sent_does_not_hide_the_failure() -> None:
    client = FakeIpcClient()

    def pipeline_that_breaks(*_args: Any) -> VoicePipeline:
        client.fail_requests = True
        raise RuntimeError("no input device")

    worker = VoiceWorker(client=client, service="voice", pipeline_factory=pipeline_that_breaks)

    with pytest.raises(WorkerStartupError, match="no input device"):
        await worker.run()
    assert client.closed


async def test_a_worker_cut_off_past_its_deadline_stops_with_the_hub_lost_code() -> None:
    client = FakeIpcClient()
    expired = asyncio.Event()

    async def deadline_sleep(_seconds: float) -> None:
        await expired.wait()

    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, sleep=deadline_sleep
    )
    task = asyncio.create_task(worker.run())
    await settle(10)
    worker.on_connection_change(True)

    worker.on_connection_change(False)
    await settle(5)
    assert not task.done()  # still inside the deadline
    expired.set()
    await asyncio.wait_for(task, 5)

    assert worker.exit_code == EXIT_HUB_LOST
    assert client.closed


async def test_a_reconnect_inside_the_deadline_keeps_the_worker() -> None:
    client = FakeIpcClient()
    expired = asyncio.Event()

    async def deadline_sleep(_seconds: float) -> None:
        await expired.wait()

    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, sleep=deadline_sleep
    )
    task = asyncio.create_task(worker.run())
    await settle(10)

    worker.on_connection_change(False)
    worker.on_connection_change(True)
    expired.set()
    await settle(10)

    assert not task.done()
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert worker.exit_code == 0


async def test_a_refused_reconnect_stops_the_worker() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    task = asyncio.create_task(worker.run())
    await settle(10)

    worker.on_reconnect_refused(IpcError(ERR_AUTH_DENIED, "invalid token"))
    await asyncio.wait_for(task, 5)

    assert worker.exit_code == EXIT_HUB_LOST
