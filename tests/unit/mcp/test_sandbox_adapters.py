"""The seams to `nox.sandbox`: picked when it can be built, never trusted when it cannot."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nox.mcp.launch import ProcessLauncher, SandboxLauncher, default_launcher
from nox.skills.testing import SandboxTestRunner, UnavailableTestRunner, default_test_runner


@dataclass(frozen=True)
class EgressDecl:
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Limits:
    timeout_s: float = 60.0
    max_message_bytes: int = 1024


@dataclass
class FakeSandbox:
    with_start: bool = True
    strong: bool = True
    runs: list[dict[str, Any]] = field(default_factory=list)

    def guarantees(self) -> Any:
        return types.SimpleNamespace(strong=self.strong)

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


def test_without_a_sandbox_module_nothing_runs_skill_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "nox.sandbox", None)
    assert isinstance(default_launcher(), ProcessLauncher)
    runner = default_test_runner()
    assert isinstance(runner, UnavailableTestRunner) and runner.unavailable()


def test_a_sandbox_that_cannot_be_built_means_no_skill_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> Any:
        raise RuntimeError("no backend")

    install(monkeypatch, broken)
    launcher, runner = default_launcher(), default_test_runner()
    assert isinstance(launcher, ProcessLauncher) and launcher.sandboxed is False
    assert isinstance(runner, UnavailableTestRunner) and runner.sandboxed is False


def test_a_weak_sandbox_is_not_good_enough_for_model_written_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, lambda: FakeSandbox(strong=False))
    assert isinstance(default_test_runner(), UnavailableTestRunner)


def test_servers_need_the_long_running_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda: FakeSandbox(with_start=False))
    assert isinstance(default_launcher(), ProcessLauncher)
    install(monkeypatch, lambda: FakeSandbox())
    assert isinstance(default_launcher(), SandboxLauncher)
    assert isinstance(default_test_runner(), SandboxTestRunner)


async def test_a_local_server_without_network_gets_no_egress(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    module = types.SimpleNamespace(EgressDecl=EgressDecl, Limits=Limits)
    launched = await SandboxLauncher(sandbox, module).launch(
        ["srv"], cwd=tmp_path / "w", env={"A": "1"}, network=False
    )
    assert launched.pid == 7 and sandbox.runs[0]["egress"] == EgressDecl()
    assert sandbox.runs[0]["env"] == {"A": "1"}
    assert "cwd" not in sandbox.runs[0]  # the sandbox's own folder, never one under <nox home>
