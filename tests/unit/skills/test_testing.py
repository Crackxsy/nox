"""`nox.skills.testing`: model-written tests run in the sandbox or nowhere, and fail closed."""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nox.skills.testing import (
    RUNNER_FILE,
    SandboxTestRunner,
    UnavailableTestRunner,
)
from tests._fake_skill_builder import write_skill


@dataclass(frozen=True)
class EgressDecl:
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 60.0


@dataclass
class FakeProcess:
    """A `SandboxProcess` stand-in that answers with a fixed stdout, stderr and exit code."""

    stdout: str
    stderr: str
    code: int | None
    sent: list[bytes] = field(default_factory=list)
    stopped: bool = False

    def __post_init__(self) -> None:
        self._lines = [line + "\n" for line in self.stdout.splitlines()]

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def readline(self) -> bytes:
        return self._lines.pop(0).encode() if self._lines else b""

    async def wait(self, timeout: float | None = None) -> int | None:
        return self.code

    async def stop(self, grace_s: float = 2.0) -> int | None:
        self.stopped = True
        return self.code

    def stderr_tail(self) -> str:
        return self.stderr


@dataclass
class FakeSandbox:
    stdout: str = "RESULT passed=2 failed=0"
    stderr: str = "PASS a"
    code: int | None = 0
    starts: list[dict[str, Any]] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)

    async def start(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.starts.append({"argv": argv, **kwargs})
        process = FakeProcess(self.stdout, self.stderr, self.code)
        self.processes.append(process)
        return process


MODULE = types.SimpleNamespace(EgressDecl=EgressDecl, Limits=Limits)


def dice(tmp_path: Path) -> Path:
    work = tmp_path / "dice"
    write_skill(work, "dice", "good")
    return work


async def test_the_tests_run_in_the_sandbox_with_their_files_on_stdin(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    outcome = await SandboxTestRunner(sandbox, MODULE).run(
        dice(tmp_path), timeout_s=30, skill_id="dice"
    )
    assert outcome.ok and outcome.passed == 2
    start = sandbox.starts[0]
    assert start["egress"] == EgressDecl() and start["lifetime_s"] == 30
    assert "cwd" not in start  # the sandbox's own private folder, nothing under <nox home>
    assert start["workload"] == "skilltest:dice"  # a sandbox container of its own (N6)
    assert start["argv"][1:3] == ["-I", "-c"] and start["argv"][-1] == RUNNER_FILE
    bundle = json.loads(sandbox.processes[0].sent[0])
    assert RUNNER_FILE in bundle and "manifest.yaml" in bundle
    assert sandbox.processes[0].stopped


async def test_tests_run_in_a_container_of_the_skill_id_not_the_folder(tmp_path: Path) -> None:
    # In production every skill is built in a folder called `work`: the container must come from
    # the skill's id, and never be the installed worker's (`skill:<id>`).
    sandbox = FakeSandbox()
    for skill_id in ("wetter", "kalender"):
        work = dice(tmp_path / skill_id).rename(tmp_path / skill_id / "work")
        await SandboxTestRunner(sandbox, MODULE).run(work, timeout_s=30, skill_id=skill_id)
    assert [start["workload"] for start in sandbox.starts] == [
        "skilltest:wetter",
        "skilltest:kalender",
    ]


@pytest.mark.parametrize(
    ("code", "stdout", "stderr"),
    [
        (1, "", "pyvenv.cfg: access denied"),  # the interpreter never started in the container
        (None, "", "PASS a"),  # `os._exit` or a kill: no trailer was written
        (0, "RESULT passed=9 failed=0\nRESULT passed=1 failed=1", ""),  # a test printed one too
        (0, "RESULT passed=1 failed=0", ""),  # counts fewer tests than the files hold
        (3, "RESULT passed=2 failed=0", ""),  # a passing trailer with a failing exit code
    ],
)
async def test_anything_but_one_clean_trailer_fails_and_nothing_runs_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int | None, stdout: str, stderr: str
) -> None:
    async def no_process(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("model-written tests were started outside the sandbox")

    monkeypatch.setattr("asyncio.create_subprocess_exec", no_process)
    sandbox = FakeSandbox(stdout=stdout, stderr=stderr, code=code)
    runner = SandboxTestRunner(sandbox, MODULE)
    for _ in range(2):
        outcome = await runner.run(dice(tmp_path), timeout_s=60, skill_id="dice")
        assert not outcome.ok and outcome.failed >= 1
    assert runner.sandboxed is True and len(sandbox.starts) == 2  # the sandbox, every time


async def test_a_sandbox_that_refuses_to_start_is_a_failed_run(tmp_path: Path) -> None:
    class Refusing(FakeSandbox):
        async def start(self, argv: list[str], **kwargs: Any) -> FakeProcess:
            raise OSError("no container")

    outcome = await SandboxTestRunner(Refusing(), MODULE).run(
        dice(tmp_path), timeout_s=60, skill_id="dice"
    )
    assert not outcome.ok and "could not run" in outcome.output


async def test_without_a_sandbox_every_run_fails_and_nothing_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_process(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("model-written tests were started outside the sandbox")

    monkeypatch.setattr("asyncio.create_subprocess_exec", no_process)
    runner = UnavailableTestRunner()
    outcome = await runner.run(dice(tmp_path), timeout_s=30, skill_id="dice")
    assert not outcome.ok and outcome.failed == 1 and runner.unavailable()
