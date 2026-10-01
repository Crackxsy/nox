"""Ending a process: the refusals, and the one thing it deliberately does not do.

`process.terminate()` on Windows is `TerminateProcess` - the program gets no chance to save. So the
tests here are mostly about what never gets that far, plus the decision not to escalate to `kill()`
when a terminate is ignored: a program that will not go is either busy saving or stuck, and guessing
which is not this code's job.
"""

from __future__ import annotations

import os
from typing import Any

import psutil
import pytest

from nox.desktop import processes


class FakeProcess:
    def __init__(self, pid: int, name: str, *, ignores_terminate: bool = False) -> None:
        self._pid = pid
        self._name = name
        self._ignores = ignores_terminate
        self.terminated = False
        self.killed = False

    def name(self) -> str:
        return self._name

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:  # pragma: no cover - reaching this is the bug the test prevents
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._ignores:
            raise psutil.TimeoutExpired(timeout or 0.0)
        return 0


def patch_process(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    """Replace the module's own lookup seam, not `psutil.Process`.

    Patching psutil itself breaks its internal `isinstance` checks, and `own_pids()` needs the real
    lookup regardless - it is what the boundary uses to refuse ending Nox.
    """

    def factory(pid: int) -> Any:
        if fake is None:
            raise psutil.NoSuchProcess(pid)
        return fake

    monkeypatch.setattr(processes, "_process", factory)


async def test_a_process_that_is_not_there_is_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_process(monkeypatch, None)

    answer = await processes.stop_process(4321, game_pids=set())

    assert answer["ok"] is False and "no process with the id 4321" in answer["error"]


async def test_windows_itself_is_never_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(600, "lsass.exe")
    patch_process(monkeypatch, fake)

    answer = await processes.stop_process(600, game_pids=set())

    assert answer["ok"] is False and "Windows itself" in answer["error"]
    assert fake.terminated is False, "the boundary has to refuse before anything is attempted"


async def test_nox_does_not_end_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(os.getpid(), "python.exe")
    patch_process(monkeypatch, fake)

    answer = await processes.stop_process(os.getpid(), game_pids=set())

    assert answer["ok"] is False and "Nox itself" in answer["error"]
    assert fake.terminated is False


async def test_the_game_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(777, "RocketLeague.exe")
    patch_process(monkeypatch, fake)

    answer = await processes.stop_process(777, game_pids={777})

    assert answer["ok"] is False and "observed only" in answer["error"]
    assert fake.terminated is False


async def test_an_ordinary_program_is_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProcess(4321, "notepad.exe")
    patch_process(monkeypatch, fake)

    answer = await processes.stop_process(4321, game_pids=set())

    assert answer["ok"] and answer["name"] == "notepad.exe"
    assert fake.terminated is True


async def test_a_program_that_ignores_the_terminate_is_not_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is either busy saving or stuck, and deciding which is not this code's call."""
    fake = FakeProcess(4321, "word.exe", ignores_terminate=True)
    patch_process(monkeypatch, fake)

    answer = await processes.stop_process(4321, game_pids=set())

    assert answer["ok"] is False and "did not end within" in answer["error"]
    assert fake.killed is False


async def test_the_listing_is_capped_and_says_the_real_total() -> None:
    """Against the real process table: a machine has hundreds, and a model gets a few."""
    answer = await processes.list_processes(limit=5)

    assert answer["ok"] and len(answer["processes"]) == 5
    assert answer["total"] > 5 and answer["truncated"] is True
    assert answer["processes"][0]["memory_mb"] >= answer["processes"][-1]["memory_mb"]


def test_nox_knows_which_processes_are_its_own() -> None:
    assert os.getpid() in processes.own_pids()


def test_a_process_windows_will_not_name_is_labelled_not_left_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protected system processes ("Registry", "Secure System") come back from psutil with an
    empty name and enough memory to make the top of the list (seen on the product owner's PC)."""

    class _Entry:
        info = {"pid": 236, "name": "", "memory_info": None}

    monkeypatch.setattr(processes.psutil, "process_iter", lambda _attrs: [_Entry()])

    answer = processes._snapshot(5)  # noqa: SLF001 - the blocking half, without a thread

    assert answer["processes"] == [
        {"pid": 236, "name": processes.UNNAMED_PROCESS, "memory_mb": 0.0}
    ]
