"""The worker's side of "voice does what it says": health in every heartbeat, mute that survives
reconnects, no model fetched without the user asking, and exactly one terminal event per
utterance (fakes; faster-whisper itself is not installed in CI)."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from nox.core.events import E, HealthStatus
from nox.voice.base import VoicePipeline
from nox.voice.models import (
    WHISPER_MODEL_FILE,
    download_kokoro,
    download_whisper,
    find_whisper_model,
)
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.voice.stt.faster_whisper_engine import FasterWhisperStt
from nox.worker.main import VoiceWorker, load_engines, parse_args
from nox.worker.voice_cli import device_lines
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeIpcClient,
    FakeStt,
    FakeTts,
    settle,
)


def real_pipeline_factory(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput, **config: Any
) -> Any:
    def factory(emit: Any, capture_allowed: Any, _config: Any = None) -> VoicePipeline:
        return DefaultVoicePipeline(
            audio_in=audio_in,
            audio_out=audio_out,
            stt=FakeStt(),
            tts=FakeTts(),
            emit=emit,
            capture_allowed=capture_allowed,
            config=PipelineConfig(**config),
        )

    return factory


async def start(worker: VoiceWorker) -> asyncio.Task[None]:
    task = asyncio.create_task(worker.run())
    await settle(10)
    return task


# ---- health reaches the core ----------------------------------------------------------------


async def test_the_heartbeat_carries_capture_wake_gate_echo_and_engine_health() -> None:
    client = FakeIpcClient()
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=real_pipeline_factory(FakeAudioInput(), FakeAudioOutput()),
        stt=FakeStt(),
        tts=FakeTts(),
        heartbeat_s=0.02,
    )
    task = await start(worker)
    await asyncio.sleep(0.06)
    beats = [p for n, p in client.requests if n == "worker.heartbeat" and "health" in p]
    assert beats, "the heartbeat must carry the health report"
    health = beats[-1]["health"]
    assert set(health) == {"capture", "wake_gate", "echo", "stt", "tts"}
    assert health["capture"]["status"] == "available"
    assert health["echo"]["status"] == "limited"
    assert "half-duplex" in health["echo"]["reason"]
    worker.request_stop()
    await task


async def test_a_dead_capture_loop_is_in_the_heartbeat() -> None:
    client = FakeIpcClient()
    audio_in = FakeAudioInput()
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=real_pipeline_factory(audio_in, FakeAudioOutput()),
        heartbeat_s=10,
    )
    task = await start(worker)
    audio_in.fail_with = OSError("PortAudio device lost")
    audio_in.push(_frame(), force=True)
    await settle(20)
    report = await worker.health_report()
    assert report["capture"]["status"] == "unavailable"
    assert "PortAudio device lost" in report["capture"]["reason"]
    worker.request_stop()
    await task


def _frame() -> Any:
    import numpy as np

    return np.zeros(480, dtype=np.float32)


# ---- mute survives reconnects and restarts --------------------------------------------------


async def test_a_restarted_worker_comes_back_muted_and_never_opens_the_mic() -> None:
    client = FakeIpcClient()
    client.register_response = {**client.register_response, "muted": True}
    audio_in = FakeAudioInput()
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=real_pipeline_factory(audio_in, FakeAudioOutput()),
        heartbeat_s=10,
    )
    task = await start(worker)
    pipeline: Any = worker.pipeline
    assert pipeline.muted
    assert audio_in.opens == 0  # continuous listening, but muted from the first frame
    worker.request_stop()
    await task


async def test_reregistering_applies_the_cores_mute_flag_both_ways() -> None:
    client = FakeIpcClient()
    audio_in = FakeAudioInput()
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=real_pipeline_factory(audio_in, FakeAudioOutput()),
        heartbeat_s=10,
    )
    task = await start(worker)
    pipeline: Any = worker.pipeline
    assert not pipeline.muted and audio_in.enabled
    client.register_response = {**client.register_response, "muted": True}
    await worker.register()  # the core was muted while this worker was disconnected
    assert pipeline.muted and not audio_in.enabled
    client.register_response = {**client.register_response, "muted": False}
    await worker.register()
    assert not pipeline.muted and audio_in.enabled
    worker.request_stop()
    await task


# ---- exactly one terminal event -------------------------------------------------------------


async def test_an_unexpected_playback_error_reports_tts_finished_exactly_once() -> None:
    client = FakeIpcClient()
    audio_out = FakeAudioOutput()

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the output device disappeared mid-sentence")

    audio_out.play = boom  # type: ignore[method-assign]
    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=real_pipeline_factory(FakeAudioInput(), audio_out),
        heartbeat_s=10,
    )
    task = await start(worker)
    await client.deliver_request("tts.speak", {"utterance_id": "u1", "text": "Hallo"})
    await settle(30)
    finished = [p for n, p in client.events if n == E.TTS_FINISHED]
    assert finished == [{"utterance_id": "u1", "ok": False, "reason": "OSError"}]
    worker.request_stop()
    await task


# ---- no model is downloaded without the user asking -----------------------------------------


def _no_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stand-in `faster_whisper` that records any attempt to build or download a model."""
    calls: list[str] = []
    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, path: str, **kwargs: Any) -> None:
            calls.append(f"model:{path}:{kwargs.get('local_files_only')}")

    def download_model(name: str, output_dir: str) -> str:
        calls.append(f"download:{name}")
        (Path(output_dir) / WHISPER_MODEL_FILE).write_bytes(b"ct2")
        return output_dir

    module.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    module.download_model = download_model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


async def test_a_missing_whisper_model_is_unavailable_with_path_and_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_faster_whisper(monkeypatch)
    stt = FasterWhisperStt(model_size="small", models_dir=str(tmp_path / "faster-whisper"))
    tts = FakeTts()
    await load_engines(stt, tts)  # does not raise: speech output still works
    status, reason = await stt.health()
    assert status is HealthStatus.UNAVAILABLE
    assert str(tmp_path / "faster-whisper" / "small") in reason
    assert "--download-whisper small" in reason
    assert calls == []  # nothing was fetched, nothing was even constructed


async def test_an_installed_whisper_model_is_loaded_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_faster_whisper(monkeypatch)
    root = tmp_path / "faster-whisper"
    (root / "small").mkdir(parents=True)
    (root / "small" / WHISPER_MODEL_FILE).write_bytes(b"ct2")
    stt = FasterWhisperStt(model_size="small", models_dir=str(root))
    await stt.load()
    assert calls == [f"model:{root / 'small'}:True"]
    assert (await stt.health())[0] is HealthStatus.AVAILABLE


def test_a_model_an_earlier_release_cached_is_still_found(tmp_path: Path) -> None:
    snapshot = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / WHISPER_MODEL_FILE).write_bytes(b"ct2")
    assert find_whisper_model(tmp_path, "small") == snapshot
    assert find_whisper_model(tmp_path, "base") is None


def test_download_whisper_fetches_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _no_faster_whisper(monkeypatch)
    lines: list[str] = []
    assert download_whisper(tmp_path, "small", echo=lines.append) == 0
    assert calls == ["download:small"]
    assert (tmp_path / "small" / WHISPER_MODEL_FILE).is_file()
    assert not (tmp_path / "small.part").exists()
    assert download_whisper(tmp_path, "small", echo=lines.append) == 0  # already there
    assert calls == ["download:small"]


@pytest.mark.parametrize("name", ["../evil", "a/b", "", ".hidden"])
def test_download_whisper_refuses_names_that_are_paths(tmp_path: Path, name: str) -> None:
    def never(_name: str, _out: Path) -> None:
        raise AssertionError("must not download")

    assert download_whisper(tmp_path, name, echo=lambda _l: None, downloader=never) == 2


def test_a_failed_whisper_download_leaves_nothing_behind(tmp_path: Path) -> None:
    def broken(_name: str, out: Path) -> None:
        (out / "partial").write_bytes(b"x")
        raise ConnectionError("offline")

    assert download_whisper(tmp_path, "small", echo=lambda _l: None, downloader=broken) == 1
    assert list(tmp_path.iterdir()) == []


def test_download_whisper_is_a_worker_flag() -> None:
    assert parse_args(["--download-whisper"]).download_whisper == ""
    assert parse_args(["--download-whisper", "base"]).download_whisper == "base"
    assert parse_args([]).download_whisper is None


def test_kokoro_download_rejects_a_truncated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, _size: int) -> list[bytes]:
            return [b"only a few bytes"]

    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *_a, **_k: Response())
    lines: list[str] = []
    assert download_kokoro(tmp_path, echo=lines.append) == 1
    assert not any(p.suffix == ".onnx" for p in tmp_path.iterdir())
    assert any("expected" in line for line in lines)


# ---- selftest ------------------------------------------------------------------------------


def test_selftest_lists_devices_on_every_platform() -> None:
    devices = [
        {
            "index": "0",
            "name": "Built-in Microphone",
            "hostapi": "Core Audio",
            "inputs": "1",
            "outputs": "0",
            "default_input": "True",
            "default_output": "False",
        }
    ]
    assert device_lines(devices, platform="darwin") == [
        "  [ 0] IN     Built-in Microphone (default)"
    ]
    assert device_lines(devices, platform="win32") == []  # Windows lists the WASAPI view only
