"""VoiceWorker message handling against a fake IPC client (no hub, no hardware)."""

from __future__ import annotations

import asyncio
import base64
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nox.core.config import VoiceConfig
from nox.core.events import E
from nox.voice.base import TtsRequest, VoicePipeline
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig
from nox.worker.main import SERVICES, VoiceWorker, decode_audio_ref, parse_args
from tests.unit.voice.conftest import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeIpcClient,
    FakeStt,
    FakeTts,
    settle,
)


class SpyPipeline:
    """Records calls; used where the pipeline internals are not under test."""

    def __init__(self, emit: Any, capture_allowed: Any) -> None:
        self.emit = emit
        self.capture_allowed = capture_allowed
        self.calls: list[tuple[str, Any]] = []
        self.refreshes = 0

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def stop(self) -> None:
        self.calls.append(("stop", None))

    async def push_to_talk(self, pressed: bool) -> None:
        self.calls.append(("ptt", pressed))

    async def set_muted(self, muted: bool) -> None:
        self.calls.append(("mute", muted))

    async def say(self, request: TtsRequest) -> None:
        self.calls.append(("say", request.utterance_id))
        await asyncio.sleep(0.01)
        await self.emit(E.TTS_FINISHED, {"utterance_id": request.utterance_id, "ok": True})

    async def interrupt(self, *, reason: str) -> None:
        self.calls.append(("interrupt", reason))

    def refresh_gate(self) -> None:
        self.refreshes += 1


def spy_factory(emit: Any, capture_allowed: Any, _config: Any = None) -> VoicePipeline:
    return SpyPipeline(emit, capture_allowed)


@pytest.fixture
def client() -> FakeIpcClient:
    return FakeIpcClient()


async def run_worker(worker: VoiceWorker) -> asyncio.Task[None]:
    task = asyncio.create_task(worker.run())
    await settle(10)
    return task


async def test_register_heartbeat_and_shutdown(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=0.02
    )
    task = await run_worker(worker)
    assert client.connected
    assert set(client.subscriptions) == {"security.*", "privacy.*", "system.stopping"}
    name, payload = client.requests[0]
    assert name == "worker.register"
    assert payload["service"] == "voice" and payload["capabilities"] == list(SERVICES["voice"])
    assert isinstance(payload["pid"], int)
    ready = [p for n, p in client.requests if n == "worker.ready"]
    assert ready == [{"service": "voice"}]
    await asyncio.sleep(0.07)
    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert len(beats) >= 2
    assert beats[-1]["status"] == "running"
    pipeline: Any = worker.pipeline
    assert ("start", None) in pipeline.calls
    worker.request_stop()
    await task
    assert client.closed
    assert ("stop", None) in pipeline.calls


async def test_service_selection_registers_only_hosted_handlers(client: FakeIpcClient) -> None:
    VoiceWorker(client=client, service="tts", pipeline_factory=spy_factory)
    assert (
        set(client.inbound) == {"tts.speak", "tts.stop", "voice.ptt", "voice.mute"}
        or client.inbound == {}
    )
    worker = VoiceWorker(client=client, service="tts", pipeline_factory=spy_factory)
    worker._register_handlers()
    assert "stt.transcribe" not in client.inbound
    assert {"tts.speak", "tts.stop", "voice.ptt", "voice.mute"} <= set(client.inbound)
    with pytest.raises(ValueError):
        VoiceWorker(client=client, service="video", pipeline_factory=spy_factory)


async def test_tts_speak_stop_ptt_mute(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=10
    )
    task = await run_worker(worker)
    pipeline: Any = worker.pipeline
    resp = await client.deliver_request("tts.speak", {"utterance_id": "u1", "text": "Hallo"})
    assert resp == {"ok": True, "utterance_id": "u1", "accepted": True}
    await asyncio.sleep(0.03)
    assert ("say", "u1") in pipeline.calls
    assert (E.TTS_FINISHED, {"utterance_id": "u1", "ok": True}) in client.events
    with pytest.raises(ValueError):
        await client.deliver_request("tts.speak", {"text": "no id"})
    assert await client.deliver_request("tts.stop", {"reason": "user"}) == {"ok": True}
    assert ("interrupt", "user") in pipeline.calls
    await client.deliver_request("voice.ptt", {"pressed": True})
    await client.deliver_request("voice.ptt", {"pressed": False})
    await client.deliver_request("voice.mute", {"muted": True})
    assert [c for c in pipeline.calls if c[0] in ("ptt", "mute")] == [
        ("ptt", True),
        ("ptt", False),
        ("mute", True),
    ]
    worker.request_stop()
    await task


async def test_kill_switch_event_stops_pipeline_and_gates(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=10
    )
    task = await run_worker(worker)
    pipeline: Any = worker.pipeline
    assert worker.capture_allowed()
    await client.deliver_event(E.SECURITY_KILL_SWITCH, {"by": "hotkey"})
    assert not worker.capture_allowed()
    assert ("interrupt", "kill_switch") in pipeline.calls and ("stop", None) in pipeline.calls
    resp = await client.deliver_request(
        "tts.speak", {"utterance_id": "u9", "text": "nach dem Notaus"}
    )
    assert resp["ok"] is False and resp["reason"] == "safe_mode"
    worker.request_stop()
    await task
    beats = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert beats == [] or beats[-1]["status"] in ("safe_mode", "running")


async def test_capture_changed_and_panic_gate(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=10
    )
    task = await run_worker(worker)
    pipeline: Any = worker.pipeline
    await client.deliver_event(
        E.PRIVACY_CAPTURE_CHANGED,
        {"microphone": False, "camera": False, "screen": True, "cloud": True},
    )
    assert not worker.capture_allowed() and pipeline.refreshes == 1
    await client.deliver_event(
        E.PRIVACY_CAPTURE_CHANGED,
        {"microphone": True, "camera": False, "screen": True, "cloud": True},
    )
    assert worker.capture_allowed() and pipeline.refreshes == 2
    await client.deliver_event(E.SECURITY_PANIC, {"origin": "tray"})
    assert not worker.capture_allowed()
    assert ("interrupt", "panic") in pipeline.calls
    worker.request_stop()
    await task


async def test_kill_phrase_emit_latches_worker(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=10
    )
    await worker.emit(
        E.VOICE_KILL_PHRASE, {"by": "voice", "reason": "kill phrase", "language": "de"}
    )
    assert client.events[-1][0] == E.VOICE_KILL_PHRASE
    assert not worker.capture_allowed() and worker.status.status == "safe_mode"
    # Never sent as a request (the hub would reject security.* from a worker anyway).
    assert all(n != "security.kill" for n, _ in client.requests)


async def test_emit_failure_is_swallowed(client: FakeIpcClient) -> None:
    worker = VoiceWorker(client=client, service="voice", pipeline_factory=spy_factory)

    async def boom(name: str, payload: Any = None, *, corr: Any = None) -> None:
        raise RuntimeError("socket closed")

    client.send_event = boom  # type: ignore[method-assign]
    await worker.emit(E.VOICE_INPUT_STARTED, {})


async def test_heartbeat_failure_keeps_running(client: FakeIpcClient) -> None:
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=0.01
    )
    task = await run_worker(worker)
    client.fail_requests = True
    await asyncio.sleep(0.05)
    assert not task.done()
    client.fail_requests = False
    worker.request_stop()
    await task


async def test_real_pipeline_through_worker_gate(client: FakeIpcClient) -> None:
    """Real DefaultVoicePipeline + fake devices: capture_changed(False) really stops frames."""
    audio_in, audio_out = FakeAudioInput(), FakeAudioOutput()

    def factory(emit: Any, capture_allowed: Any, _config: Any = None) -> VoicePipeline:
        return DefaultVoicePipeline(
            audio_in=audio_in,
            audio_out=audio_out,
            stt=FakeStt(),
            tts=FakeTts(),
            emit=emit,
            capture_allowed=capture_allowed,
            config=PipelineConfig(),
        )

    worker = VoiceWorker(client=client, service="voice", pipeline_factory=factory, heartbeat_s=10)
    task = await run_worker(worker)
    assert audio_in.enabled is True
    await client.deliver_event(
        E.PRIVACY_CAPTURE_CHANGED,
        {"microphone": False, "camera": False, "screen": False, "cloud": False},
    )
    assert audio_in.enabled is False
    worker.request_stop()
    await task


async def test_stt_transcribe_request(client: FakeIpcClient, tmp_path: Path) -> None:
    stt = FakeStt("hallo welt")
    worker = VoiceWorker(client=client, service="stt", pipeline_factory=spy_factory, stt=stt)
    worker._register_handlers()
    pcm = (np.sin(np.linspace(0, 200, 16000)) * 10000).astype(np.int16)
    wav = tmp_path / "in.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())
    resp = await client.deliver_request("stt.transcribe", {"audio_ref": str(wav), "language": "de"})
    assert resp["text"] == "hallo welt" and resp["duration_ms"] == 1000 and resp["language"] == "de"
    resp = await client.deliver_request(
        "stt.transcribe",
        {"audio_b64": base64.b64encode(pcm[:8000].tobytes()).decode(), "sample_rate": 16000},
    )
    assert resp["duration_ms"] == 500
    with pytest.raises(FileNotFoundError):
        decode_audio_ref({"audio_ref": str(tmp_path / "missing.wav")})
    with pytest.raises(FileNotFoundError):
        decode_audio_ref({"audio_ref": str(tmp_path / "notes.txt")})
    with pytest.raises(ValueError):
        decode_audio_ref({})


async def test_registered_config_overrides_defaults(client: FakeIpcClient) -> None:
    """B-8: the config the core returns from `worker.register` is applied, not the local default."""
    client.register_response = {"ok": True, "config": {"stt": {"model": "base"}}}
    loaded: list[VoiceConfig] = []

    async def component_loader(cfg: VoiceConfig) -> tuple[Any, Any]:
        loaded.append(cfg)
        return FakeStt(), FakeTts()

    worker = VoiceWorker(
        client=client,
        service="voice",
        pipeline_factory=spy_factory,
        heartbeat_s=10,
        component_loader=component_loader,
    )
    assert worker.voice_config.stt.model == "small"  # local default, before registering
    task = await run_worker(worker)
    assert worker.voice_config.stt.model == "base"  # overridden by the core's response
    assert loaded == [worker.voice_config]  # engines were built from the *resolved* config
    assert isinstance(worker.stt, FakeStt) and isinstance(worker.tts, FakeTts)
    worker.request_stop()
    await task


async def test_invalid_registered_config_falls_back_to_defaults(client: FakeIpcClient) -> None:
    client.register_response = {"ok": True, "config": {"stt": {"model": 123}}}
    worker = VoiceWorker(
        client=client, service="voice", pipeline_factory=spy_factory, heartbeat_s=10
    )
    task = await run_worker(worker)
    assert worker.voice_config == VoiceConfig()  # fell back to the local default
    worker.request_stop()
    await task


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOX_HUB_URL", raising=False)
    args = parse_args([])
    assert (
        args.service == "voice" and args.hub_url == "ws://127.0.0.1:47800/ws" and not args.selftest
    )
    monkeypatch.setenv("NOX_HUB_URL", "ws://127.0.0.1:1/ws")
    assert parse_args(["--service", "stt"]).hub_url == "ws://127.0.0.1:1/ws"


def test_main_refuses_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.worker.main import main

    monkeypatch.delenv("NOX_WORKER_TOKEN", raising=False)
    monkeypatch.setattr("nox.worker.main.raise_priority", lambda: None)
    assert main(["--service", "voice"]) == 2
