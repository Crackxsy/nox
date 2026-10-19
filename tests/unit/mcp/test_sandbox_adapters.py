"""The seams to `nox.sandbox`: picked when it can be built, never trusted when it cannot."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nox.mcp.launch import ProcessLauncher, SandboxLauncher, default_launcher
from nox.skills.testing import ProcessTestRunner, SandboxTestRunner, default_test_runner
from tests._fake_skill_builder import write_skill


@dataclass(frozen=True)
class EgressDecl:
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 60.0


@dataclass
class Result:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class FakeSandbox:
    with_start: bool = True
    runs: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, argv: list[str], **kwargs: Any) -> Result:
        self.runs.append({"argv": argv, **kwargs})
        assert (Path(kwargs["cwd"]) / argv[-1]).is_file()  # the runner is in the confined folder
        return Result(0, "PASS a\nRESULT passed=2 failed=0", "")

    def __getattr__(self, name: str) -> Any:
        if name == "start" and self.with_start:
            return self._start
        raise AttributeError(name)

    async def _start(self, argv: list[str], **kwargs: Any) -> Any:
        self.runs.append({"argv": argv, **kwargs})
        return types.SimpleNamespace(stdin=None, stdout=None, pid=7, returncode=None)


def install(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    module = types.ModuleType("nox.sandbox")
    module.make_sandbox = factory  # type: ignore[attr-defined]
    module.EgressDecl = EgressDecl  # type: ignore[attr-defined]
    module.Limits = Limits  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nox.sandbox", module)


def test_without_a_sandbox_module_the_process_runners_are_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "nox.sandbox", None)
    assert isinstance(default_launcher(), ProcessLauncher)
    assert isinstance(default_test_runner(), ProcessTestRunner)


def test_a_sandbox_that_cannot_be_built_falls_back_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> Any:
        raise RuntimeError("no backend")

    install(monkeypatch, broken)
    launcher, runner = default_launcher(), default_test_runner()
    assert isinstance(launcher, ProcessLauncher) and launcher.sandboxed is False
    assert isinstance(runner, ProcessTestRunner) and runner.sandboxed is False


def test_servers_need_the_long_running_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda: FakeSandbox(with_start=False))
    assert isinstance(default_launcher(), ProcessLauncher)
    install(monkeypatch, lambda: FakeSandbox())
    assert isinstance(default_launcher(), SandboxLauncher)
    assert isinstance(default_test_runner(), SandboxTestRunner)


async def test_skill_tests_run_inside_the_sandbox_without_egress(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    work = tmp_path / "dice"
    write_skill(work, "dice", "good")
    module = types.SimpleNamespace(EgressDecl=EgressDecl, Limits=Limits)
    outcome = await SandboxTestRunner(sandbox, module).run(work, timeout_s=30)
    assert outcome.ok and outcome.passed == 2
    run = sandbox.runs[0]
    assert run["egress"] == EgressDecl() and run["limits"].timeout_s == 30
    assert run["argv"][1] == "-I"


async def test_a_local_server_without_network_gets_no_egress(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    module = types.SimpleNamespace(EgressDecl=EgressDecl, Limits=Limits)
    launched = await SandboxLauncher(sandbox, module).launch(
        ["srv"], cwd=tmp_path / "w", env={"A": "1"}, network=False
    )
    assert launched.pid == 7 and sandbox.runs[0]["egress"] == EgressDecl()
    assert sandbox.runs[0]["env"] == {"A": "1"}
