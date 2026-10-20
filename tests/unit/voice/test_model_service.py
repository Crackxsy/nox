"""`voice.model.*`: the dashboard's download runs as a subprocess, one at a time, refused in
offline/private before anything starts, and every outcome is said as it is. No process is ever
spawned here - `spawn` is a fake that replays JSON lines."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.events import Event
from nox.core.events.names import E
from nox.ipc.dispatch import EmptyPayload, RequestRegistry
from nox.voice import install as voice_install
from nox.voice.models import SUPERTONIC_FILES
from nox.voice.service import VoiceModelService, register_voice_model_ipc

CTX: Any = SimpleNamespace(role="dashboard", client_id="dashboard:test")


class FakeStdout:
    def __init__(self, lines: list[bytes], gate: asyncio.Event | None) -> None:
        self.lines = list(lines)
        self.gate = gate

    async def readline(self) -> bytes:
        if self.gate is not None:
            await self.gate.wait()
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, lines: list[bytes], code: int, gate: asyncio.Event | None) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.stdout = FakeStdout(lines, gate)
        self.code = code
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self.code
        return self.code

    def terminate(self) -> None:
        # Like a real process: its stdout ends, and it exits with a failure code.
        self.terminated = True
        self.code = 1
        self.stdout.lines.clear()
        if self.stdout.gate is not None:
            self.stdout.gate.set()


def _json(**payload: Any) -> bytes:
    return (json.dumps(payload) + "\n").encode()


def _fill(directory: Path) -> None:
    for name in SUPERTONIC_FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


class Harness:
    def __init__(self, tmp_path: Path, *, mode: str = "balanced", engine: str = "supertonic"):
        self.root = tmp_path / "models"
        self.mode = mode
        self.engine = engine
        self.spawned: list[list[str]] = []
        self.joined: list[int] = []
        self.lines: list[bytes] = []
        self.code = 0
        self.fill_on_success = True
        self.gate: asyncio.Event | None = None
        self.process: FakeProcess | None = None
        self.service = VoiceModelService(
            engine=lambda: self.engine,
            models_root=lambda: self.root,
            privacy_mode=lambda: self.mode,
            spawn=self.spawn,
            python="python-test",
            on_spawn=self.joined.append,
        )

    async def spawn(self, argv: Sequence[str]) -> FakeProcess:
        self.spawned.append(list(argv))
        if self.code == 0 and self.fill_on_success:
            _fill(self.root / "supertonic")
        self.process = FakeProcess(self.lines, self.code, self.gate)
        return self.process

    async def finished(self) -> dict[str, Any]:
        for _ in range(200):
            if not self.service.running:
                return self.service.status()
            await asyncio.sleep(0.01)
        raise AssertionError("the download did not finish")


async def test_status_names_the_engine_the_missing_files_and_the_size(tmp_path: Path) -> None:
    status = Harness(tmp_path).service.status()
    assert status["engine"] == "supertonic" and status["configured_engine"] == "supertonic"
    assert not status["present"] and len(status["missing"]) == len(SUPERTONIC_FILES)
    assert status["size_bytes"] > 390_000_000 and status["license"] == "OpenRAIL-M"
    assert status["running"] is False and status["outcome"] is None
    assert status["download_blocked"] is False and status["restart_required"] is False


async def test_a_download_runs_the_worker_and_reports_progress(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.gate = asyncio.Event()
    h.lines = [
        b"noise that is not json\n",
        _json(
            type="progress",
            file="onnx/a",
            file_done=5,
            file_total=10,
            done=5,
            total=20,
            index=1,
            count=2,
        ),
        _json(type="result", code=0),
    ]
    started = h.service.download()
    assert started["ok"] is True and started["running"] is True
    await asyncio.sleep(0)
    assert h.spawned == [
        [
            "python-test",
            "-m",
            "nox.worker",
            "--download-supertonic",
            "--progress-json",
            "--models-dir",
            str(h.root),
        ]
    ]
    again = h.service.download()
    assert again["ok"] is False and again["reason"] == "running"
    assert len(h.spawned) == 1  # one run at a time, never queued
    h.gate.set()
    status = await h.finished()
    assert status["outcome"] == "done" and status["present"] is True
    assert status["restart_required"] is True
    assert status["progress"]["done"] == 5 and status["progress"]["total"] == 20
    assert h.joined == [4242]  # the downloader joins the workers' job object


@pytest.mark.parametrize("mode", ["offline", "private"])
async def test_offline_and_private_refuse_before_anything_starts(tmp_path: Path, mode: str) -> None:
    h = Harness(tmp_path, mode=mode)
    assert h.service.status()["download_blocked"] is True
    reply = h.service.download()
    assert reply["ok"] is False and reply["reason"] == "privacy_blocked"
    assert reply["outcome"] == "privacy_blocked"
    assert h.spawned == [] and not h.service.running


async def test_files_already_present_start_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _fill(h.root / "supertonic")
    reply = h.service.download()
    assert reply["ok"] is True and reply["outcome"] == "already_present"
    assert h.spawned == []


async def test_a_failed_download_carries_the_reason(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.code = 1
    h.lines = [
        _json(type="log", text="failed   onnx/vocoder.onnx: ConnectError: no route"),
        _json(type="result", code=1),
    ]
    h.service.download()
    status = await h.finished()
    assert status["outcome"] == "failed"
    assert "ConnectError" in status["detail"] and status["restart_required"] is False


async def test_success_without_the_files_is_not_called_done(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.fill_on_success = False
    h.lines = [_json(type="result", code=0)]
    h.service.download()
    status = await h.finished()
    assert status["outcome"] == "failed" and "still missing" in status["detail"]


async def test_a_process_that_cannot_start_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    async def broken(_argv: Sequence[str]) -> FakeProcess:
        raise FileNotFoundError("python-test")

    service = VoiceModelService(
        engine=lambda: "supertonic",
        models_root=lambda: tmp_path,
        privacy_mode=lambda: "balanced",
        spawn=broken,
    )
    service.download()
    for _ in range(100):
        if not service.running:
            break
        await asyncio.sleep(0.01)
    assert service.outcome == "failed" and "FileNotFoundError" in service.detail


async def test_stop_terminates_a_running_download(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.gate = asyncio.Event()
    h.service.download()
    await asyncio.sleep(0.01)
    await h.service.stop()
    assert h.process is not None and h.process.terminated
    assert not h.service.running


async def test_kokoro_configured_downloads_kokoro(tmp_path: Path) -> None:
    h = Harness(tmp_path, engine="kokoro")
    h.fill_on_success = False
    h.service.download()
    await h.finished()
    assert "--download-kokoro" in h.spawned[0]


def test_only_the_shell_and_the_dashboard_may_call_them(tmp_path: Path) -> None:
    registry = RequestRegistry()
    register_voice_model_ipc(registry, Harness(tmp_path).service)
    for name in ("voice.model.status", "voice.model.download"):
        registration = registry.get(name)
        assert registration is not None
        assert registration.allowed_roles == frozenset({"shell", "dashboard"})


async def test_install_reads_engine_root_and_privacy_from_the_core(tmp_path: Path) -> None:
    registry = RequestRegistry()
    core: Any = SimpleNamespace(
        config=SimpleNamespace(
            voice=SimpleNamespace(tts=SimpleNamespace(engine="supertonic"), models_dir=""),
            paths=SimpleNamespace(data_dir=str(tmp_path)),
            privacy=SimpleNamespace(mode="balanced"),
        ),
        security=SimpleNamespace(privacy=SimpleNamespace(mode=SimpleNamespace(value="offline"))),
        registry=registry,
    )
    service = voice_install.install(core)
    registration = registry.get("voice.model.status")
    assert registration is not None
    status = await registration.handler(CTX, EmptyPayload())
    assert status["directory"] == str(tmp_path / "models" / "supertonic")
    assert status["privacy_mode"] == "offline" and status["download_blocked"] is True
    assert service.download()["reason"] == "privacy_blocked"


class FakeBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def subscribe(self, pattern: str, handler: Any) -> Any:
        self.handlers.setdefault(pattern, []).append(handler)
        return lambda: self.handlers[pattern].remove(handler)

    async def publish(self, name: str, payload: dict[str, Any]) -> None:
        for handler in list(self.handlers.get(name, [])):
            await handler(Event(name=name, payload=payload))


def _privacy_event(current: str) -> dict[str, Any]:
    return {"previous": "balanced", "current": current, "by": "user"}


@pytest.mark.parametrize("mode", ["offline", "private"])
async def test_a_privacy_switch_terminates_a_running_download(tmp_path: Path, mode: str) -> None:
    h = Harness(tmp_path)
    h.fill_on_success = False
    h.gate = asyncio.Event()  # the downloader is mid-file and prints nothing more
    bus = FakeBus()
    h.service.watch(bus)
    part = h.root / "supertonic" / "onnx" / "vocoder.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"half a model")
    h.service.download()
    await asyncio.sleep(0.01)
    assert h.service.running
    h.mode = mode
    await bus.publish(E.PRIVACY_MODE_CHANGED, _privacy_event(mode))
    assert h.process is not None and h.process.terminated
    assert not h.service.running
    status = h.service.status()
    assert status["outcome"] == "privacy_blocked" and status["detail"] == mode
    assert status["progress"] is None
    assert not part.exists()  # nothing half-written is left behind


async def test_a_switch_to_a_permissive_mode_lets_the_download_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.gate = asyncio.Event()
    h.lines = [_json(type="result", code=0)]
    bus = FakeBus()
    h.service.watch(bus)
    h.service.download()
    await asyncio.sleep(0.01)
    await bus.publish(E.PRIVACY_MODE_CHANGED, _privacy_event("full"))
    assert h.process is not None and not h.process.terminated
    h.gate.set()
    status = await h.finished()
    assert status["outcome"] == "done"


async def test_a_privacy_switch_without_a_download_changes_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert await h.service.abort_for_privacy("offline") is False
    assert h.service.outcome is None


async def test_stop_unsubscribes_from_the_bus(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    bus = FakeBus()
    h.service.watch(bus)
    assert bus.handlers[E.PRIVACY_MODE_CHANGED]
    await h.service.stop()
    assert bus.handlers[E.PRIVACY_MODE_CHANGED] == []


async def test_install_watches_privacy_changes_on_the_core_bus(tmp_path: Path) -> None:
    bus = FakeBus()
    core: Any = SimpleNamespace(
        config=SimpleNamespace(
            voice=SimpleNamespace(tts=SimpleNamespace(engine="supertonic"), models_dir=""),
            paths=SimpleNamespace(data_dir=str(tmp_path)),
            privacy=SimpleNamespace(mode="balanced"),
        ),
        registry=RequestRegistry(),
        bus=bus,
    )
    service = voice_install.install(core)
    assert len(bus.handlers[E.PRIVACY_MODE_CHANGED]) == 1
    await service.stop()
