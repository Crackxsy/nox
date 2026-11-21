"""The voice worker with a real pipeline on fakes: stopping a turn.

`test_worker.py` drives the worker against a spy pipeline to pin the plumbing. Here the pipeline is
the real one, because what matters is what the user hears: after `tts.stop` nothing of the stopped
turn plays, and the next `tts.speak` does.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nox.core.events import E
from nox.voice.base import VoicePipeline
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.worker.main import VoiceWorker
from tests.unit.voice.conftest import FakeAudioInput, FakeIpcClient, FakeStt, FakeTts, settle
from tests.unit.voice.test_interrupt_generations import HeldOutput


class Built:
    """What the factory made, so a test can reach the fakes behind the real pipeline."""

    def __init__(self) -> None:
        self.audio_in = FakeAudioInput()
        self.audio_out = HeldOutput()
        self.pipeline: DefaultVoicePipeline | None = None

    def factory(self, emit: Any, capture_allowed: Any, _config: Any = None) -> VoicePipeline:
        self.pipeline = DefaultVoicePipeline(
            audio_in=self.audio_in,
            audio_out=self.audio_out,
            stt=FakeStt(),
            tts=FakeTts(),
            emit=emit,
            capture_allowed=capture_allowed,
            config=PipelineConfig(),
        )
        return self.pipeline


@pytest.fixture
def client() -> FakeIpcClient:
    return FakeIpcClient()


async def start(worker: VoiceWorker) -> asyncio.Task[None]:
    task = asyncio.create_task(worker.run())
    await settle(40)
    return task


def speak(utterance_id: str) -> dict[str, Any]:
    return {"utterance_id": utterance_id, "text": f"Satz {utterance_id}"}


def events_of(client: FakeIpcClient, name: str) -> list[str]:
    return [p["utterance_id"] for n, p in client.events if n == name]


async def test_tts_stop_silences_the_accepted_sentences_and_the_next_turn_still_speaks(
    client: FakeIpcClient,
) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=60.0
    )
    task = await start(worker)

    for name in ("t1-a", "t1-b", "t1-c"):  # the core sends a turn's sentences one after another
        reply = await client.deliver_request("tts.speak", speak(name))
        assert reply["accepted"] is True
    await built.audio_out.playing.wait()
    await settle()

    await client.deliver_request("tts.stop", {"reason": "barge_in"})
    await settle()
    assert built.audio_out.played_ids() == ["t1-a"]  # the two waiting sentences never played
    assert events_of(client, E.TTS_STARTED) == ["t1-a"]
    assert events_of(client, E.TTS_INTERRUPTED) == ["t1-a", "t1-b", "t1-c"]

    built.audio_out.release.set()
    await client.deliver_request("tts.speak", speak("t2-a"))
    await settle()
    assert built.audio_out.played_ids() == ["t1-a", "t2-a"]
    assert events_of(client, E.TTS_FINISHED) == ["t2-a"]

    worker.request_stop()
    await task
