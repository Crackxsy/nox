"""The voice worker's device requests: listing outputs for Settings and switching them live."""

from __future__ import annotations

from typing import Any

import pytest

from nox.voice import audio
from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker import run_worker, spy_factory


class _Output:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_devices(self, *, private_device: str, stream_device: str) -> None:
        self.calls.append((private_device, stream_device))


async def test_devices_set_switches_the_worker_output() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    output = _Output()
    worker.audio_output = output
    task = await run_worker(worker)
    reply = await client.deliver_request(
        "voice.devices.set", {"private_device": "", "stream_device": "CABLE Input"}
    )
    assert reply == {"ok": True}
    assert output.calls == [("", "CABLE Input")]
    worker.request_stop()
    await task


async def test_devices_list_reports_output_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "output_device_names", lambda: ["CABLE Input", "Headphones"])
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    task = await run_worker(worker)
    reply: Any = await client.deliver_request("voice.devices.list", {})
    assert reply == {"outputs": ["CABLE Input", "Headphones"]}
    worker.request_stop()
    await task


async def test_devices_set_without_an_output_says_so() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)
    task = await run_worker(worker)
    reply = await client.deliver_request("voice.devices.set", {"stream_device": "x"})
    assert reply["ok"] is False
    worker.request_stop()
    await task
