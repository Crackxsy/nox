"""What the voice worker tells the core about itself: the microphone's state, in every heartbeat.

Same setup as `test_worker_speech.py` - the real pipeline on fakes behind a `VoiceWorker`.
"""

from __future__ import annotations

import asyncio

import pytest

from nox.core.events import E, HealthStatus
from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeIpcClient
from tests.unit.voice.test_worker_speech import Built, start


@pytest.fixture
def client() -> FakeIpcClient:
    return FakeIpcClient()


async def test_the_heartbeat_reports_a_working_microphone(client: FakeIpcClient) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=0.01
    )
    task = await start(worker)
    await asyncio.sleep(0.05)

    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert beats and beats[-1]["capture"] == str(HealthStatus.AVAILABLE)
    assert beats[-1]["capture_reason"] == "capturing"

    worker.request_stop()
    await task


async def test_the_heartbeat_reports_a_microphone_that_cannot_be_opened(
    client: FakeIpcClient,
) -> None:
    built = Built()
    built.audio_in.open_error = OSError("no input device")
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=0.01
    )
    task = await start(worker)
    await asyncio.sleep(0.05)

    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert beats[-1]["capture"] == str(HealthStatus.UNAVAILABLE)
    assert "no input device" in beats[-1]["capture_reason"]

    worker.request_stop()
    await task


async def test_a_worker_in_safe_mode_does_not_report_its_stopped_microphone_as_a_failure(
    client: FakeIpcClient,
) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=0.01
    )
    task = await start(worker)
    await client.deliver_event(E.SECURITY_KILL_SWITCH, {})
    mark = len(client.requests)
    await asyncio.sleep(0.05)

    beats = [p for n, p in client.requests[mark:] if n == "worker.heartbeat"]
    assert beats
    assert all("capture" not in beat for beat in beats)
    assert beats[-1]["status"] == "safe_mode"

    worker.request_stop()
    await task


class FakeChain:
    def __init__(self, note: str) -> None:
        self.note = note

    def notes(self) -> str:
        return self.note


async def test_the_heartbeat_carries_the_input_side_note(client: FakeIpcClient) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=0.01
    )
    worker.capture_chain = FakeChain("speech detection: energy fallback (x)")
    task = await start(worker)
    await asyncio.sleep(0.05)

    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert beats[-1]["capture_hint"] == "speech detection: energy fallback (x)"
    assert beats[-1]["capture"] == str(HealthStatus.AVAILABLE)

    worker.request_stop()
    await task


async def test_a_worker_without_an_input_chain_sends_no_note(client: FakeIpcClient) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=0.01
    )
    task = await start(worker)
    await asyncio.sleep(0.05)

    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert beats and all("capture_hint" not in beat for beat in beats)

    worker.request_stop()
    await task
