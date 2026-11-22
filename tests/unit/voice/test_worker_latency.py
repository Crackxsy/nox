"""`voice.latency` on the worker: the pipeline's turn timings as the answer to a request."""

from __future__ import annotations

import pytest

from nox.voice.pipeline import PipelineConfig
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


async def test_the_latency_reply_carries_the_silence_between_sentences(
    client: FakeIpcClient,
) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=60.0
    )
    task = await start(worker)
    assert built.pipeline is not None
    for gap_s in (0.0, 0.02, 0.05, 0.3):  # the fourth is a stutter
        built.pipeline._latency.record_gap(0.0, gap_s)  # noqa: SLF001

    reply = await client.deliver_request("voice.latency", {})

    assert reply["sentence_gaps"] == 4
    assert reply["sentence_gap_ms"] == {"p50": 20.0, "p95": 300.0}
    assert reply["sentence_gap_budget_ms"] == 120.0
    assert reply["sentence_gap_ok"] is False

    worker.request_stop()
    await task


async def test_the_heartbeat_reports_the_gap_once_there_is_one(client: FakeIpcClient) -> None:
    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=60.0
    )
    task = await start(worker)
    assert built.pipeline is not None

    assert "speech_gap_p95_ms" not in worker._capture_report()  # noqa: SLF001 - nothing spoken yet
    built.pipeline._latency.record_gap(0.0, 0.08)  # noqa: SLF001
    built.pipeline._latency.record_gap(0.0, 0.01)  # noqa: SLF001

    report = worker._capture_report()  # noqa: SLF001
    assert report["speech_gap_p95_ms"] == 80.0 and report["speech_gaps"] == 2
    assert report["capture"] == "available"

    worker.request_stop()
    await task


async def test_shutdown_closes_the_output_streams_that_were_kept_open(
    client: FakeIpcClient,
) -> None:
    class Output:
        closed = 0

        async def close(self) -> None:
            self.closed += 1

    built = Built()
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=built.factory, heartbeat_s=60.0
    )
    task = await start(worker)
    worker.audio_output = output = Output()

    worker.request_stop()
    await task
    await worker.shutdown()

    assert output.closed >= 1


def test_the_configuration_reaches_the_pipeline() -> None:
    from nox.core.config import VoiceConfig
    from nox.worker.main import pipeline_config_from

    voice = VoiceConfig.model_validate(
        {
            "barge_in": False,
            "barge_in_min_ms": 250,
            "barge_in_min_db": -30.0,
            "tts": {"prefetch_sentences": 2, "prefetch_buffer_s": 12.5, "edge_fade_ms": 6.0},
        }
    )

    config = pipeline_config_from(voice)

    assert (config.barge_in, config.barge_in_min_ms, config.barge_in_min_db) == (False, 250, -30.0)
    assert (config.prefetch_sentences, config.prefetch_buffer_s, config.edge_fade_ms) == (
        2,
        12.5,
        6.0,
    )


def test_the_defaults_are_the_same_in_the_file_and_in_the_pipeline() -> None:
    from nox.core.config import VoiceConfig
    from nox.worker.main import pipeline_config_from

    config = pipeline_config_from(VoiceConfig())

    assert config.prefetch_sentences == PipelineConfig().prefetch_sentences == 1
    assert config.barge_in_min_ms == PipelineConfig().barge_in_min_ms
    assert config.barge_in_min_db == PipelineConfig().barge_in_min_db
    assert config.edge_fade_ms == PipelineConfig().edge_fade_ms
    assert config.prefetch_buffer_s == PipelineConfig().prefetch_buffer_s
