"""A second core on the same runtime directory exits before it touches anything."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nox import entrypoints
from nox.core.instance_lock import InstanceLock


class StubCore:
    """The part of `NoxCore` the entry point drives."""

    def __init__(self, runtime_dir: Path) -> None:
        self.config = SimpleNamespace(paths=SimpleNamespace(runtime_dir=runtime_dir))
        self.shutdown_requested = asyncio.Event()
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        (Path(self.config.paths.runtime_dir) / "session.token").write_text("mine", "utf-8")
        self.shutdown_requested.set()

    async def stop(self) -> None:
        self.calls.append("stop")


async def test_a_second_core_exits_without_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(entrypoints, "CORE_LOCK_WAIT_S", 0.0)
    (tmp_path / "session.token").write_text("running-core-token", encoding="utf-8")
    running = InstanceLock(tmp_path, "core")
    running.acquire()
    second = StubCore(tmp_path)
    try:
        code = await entrypoints._run(cast(Any, second))
    finally:
        running.release()

    assert code == entrypoints.EXIT_ALREADY_RUNNING
    assert second.calls == []
    assert (tmp_path / "session.token").read_text(encoding="utf-8") == "running-core-token"
    assert "already running" in capsys.readouterr().err


async def test_a_single_core_runs_and_releases_the_lock(tmp_path: Path) -> None:
    core = StubCore(tmp_path)

    code = await entrypoints._run(cast(Any, core))

    assert code == 0
    assert core.calls == ["start", "stop"]
    after = InstanceLock(tmp_path, "core")
    after.acquire()  # released on the way out
    after.release()
