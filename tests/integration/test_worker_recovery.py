"""Fault injection: the voice worker is dropped by the hub, killed, or cannot load its model.

The real core spawns a real worker process (`fake_voice_worker.py`: the real `VoiceWorker` and IPC
client, no audio). What the user sees is health: it must name the failure and show the worker back.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import NoxConfig, load_config
from nox.core.events import HealthStatus
from tests._ports import free_port_base

FAKE_WORKER = Path(__file__).with_name("fake_voice_worker.py")


def _config(tmp_path: Path) -> NoxConfig:
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    return load_config(
        DEFAULTS_PATH,
        None,
        None,
        {
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "vault_dir": str(tmp_path / "vault"),
                "runtime_dir": str(tmp_path / "runtime"),
            },
            "ipc": {"port": base, "http_port": base + 1},
            "privacy": {"mode": "offline"},
            "health": {"check_interval_s": 3600},
            "logging": {"level": "WARNING"},
        },
    )


def _core(tmp_path: Path) -> NoxCore:
    return NoxCore(
        _config(tmp_path),
        voice=True,
        profiles_dir=PROFILES_DIR,
        worker_command=[sys.executable, str(FAKE_WORKER)],
        extensions=False,
    )


async def _stop(core: NoxCore) -> None:
    await asyncio.wait_for(core.stop(), timeout=30)


async def wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _voice(core: NoxCore) -> tuple[HealthStatus, str]:
    return core.workers.health("voice")


def _pid(core: NoxCore) -> int | None:
    worker = core.workers.get("voice")
    return worker.process.pid if worker is not None and worker.process is not None else None


async def test_a_worker_the_hub_dropped_comes_back_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_VOICE_MODE", "ok")
    core = _core(tmp_path)
    await asyncio.wait_for(core.start(), 60)
    try:
        await wait_until(lambda: _voice(core)[0] is HealthStatus.AVAILABLE)
        pid = _pid(core)
        assert core.hub is not None

        await core.hub.disconnect("worker:voice", "event backlog")

        await wait_until(lambda: core.workers.client_id("voice") is not None)
        assert _voice(core) == (HealthStatus.AVAILABLE, "worker registered")
        assert _pid(core) == pid  # the same process reconnected; nothing was respawned
    finally:
        await _stop(core)


async def test_a_killed_worker_is_respawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_VOICE_MODE", "ok")
    core = _core(tmp_path)
    await asyncio.wait_for(core.start(), 60)
    try:
        await wait_until(lambda: _voice(core)[0] is HealthStatus.AVAILABLE)
        first = _pid(core)
        assert first is not None

        psutil.Process(first).kill()

        await wait_until(lambda: _pid(core) not in (None, first))
        await wait_until(lambda: _voice(core)[0] is HealthStatus.AVAILABLE)
    finally:
        await _stop(core)


async def test_a_missing_model_is_named_in_health_and_retried_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_VOICE_MODE", "missing_model")
    core = _core(tmp_path)
    await asyncio.wait_for(core.start(), 60)
    try:
        first = _pid(core)
        await wait_until(lambda: "restarting in" in _voice(core)[1])
        status, reason = _voice(core)
        assert status is HealthStatus.UNAVAILABLE
        assert "exited with code 1" in reason
        assert "FileNotFoundError: voice model missing: de_DE.onnx" in reason
        assert "/opt/nox-test" not in reason  # /health is unauthenticated: no paths

        await wait_until(lambda: _pid(core) not in (None, first))  # tried again
    finally:
        await _stop(core)
