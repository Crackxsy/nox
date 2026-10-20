"""The seams to `nox.sandbox`: picked when it can be built, never trusted when it cannot."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nox.mcp.client import McpClientError
from nox.mcp.launch import ProcessLauncher, RefusingLauncher, SandboxLauncher, default_launcher
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
    assert isinstance(default_launcher(), RefusingLauncher)
    runner = default_test_runner()
    assert isinstance(runner, UnavailableTestRunner) and runner.unavailable()


def test_a_sandbox_that_cannot_be_built_means_no_skill_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**_: Any) -> Any:
        raise RuntimeError("no backend")

    install(monkeypatch, broken)
    launcher, runner = default_launcher(), default_test_runner()
    assert isinstance(launcher, RefusingLauncher) and launcher.sandboxed is False
    assert isinstance(runner, UnavailableTestRunner) and runner.sandboxed is False


def test_a_weak_sandbox_is_not_good_enough_for_model_written_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, lambda **_: FakeSandbox(strong=False))
    assert isinstance(default_test_runner(), UnavailableTestRunner)


def test_servers_need_the_long_running_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda **_: FakeSandbox(with_start=False))
    assert isinstance(default_launcher(), RefusingLauncher)
    install(monkeypatch, lambda **_: FakeSandbox())
    assert isinstance(default_launcher(), SandboxLauncher)
    assert isinstance(default_test_runner(), SandboxTestRunner)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("bwrap is not installed"),
        RuntimeError("bwrap does not match the pinned hash"),
        OSError("the user-namespace probe failed"),
    ],
)
async def test_a_sandbox_that_cannot_be_built_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    def broken(**_: Any) -> Any:
        raise failure

    install(monkeypatch, broken)
    launcher = default_launcher()
    assert isinstance(launcher, RefusingLauncher) and launcher.sandboxed is False
    assert str(failure) in launcher.refusal and "allow_unsandboxed" in launcher.refusal
    with pytest.raises(McpClientError):
        await launcher.launch(["srv"], cwd=tmp_path / "w", env={}, network=False)
    assert not (tmp_path / "w").exists()


def test_only_the_explicit_opt_in_runs_servers_without_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**_: Any) -> Any:
        raise RuntimeError("no backend")

    install(monkeypatch, broken)
    launcher = default_launcher(allow_unsandboxed=True)
    assert isinstance(launcher, ProcessLauncher) and launcher.sandboxed is False


def test_the_sandbox_gets_the_same_network_guard_and_audit_as_a_shell_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSandbox:
        seen.append(kwargs)
        return FakeSandbox()

    def audit(_decision: Any) -> None:
        return None

    install(monkeypatch, factory)
    launcher = default_launcher(
        home_assistant_ips=["192.0.2.10"], blocked_hosts=["ha.local"], audit=audit
    )
    assert isinstance(launcher, SandboxLauncher)
    assert seen[0]["home_assistant_ips"] == ("192.0.2.10",)
    assert seen[0]["blocked_hosts"] == ("ha.local",)
    assert seen[0]["audit"] is audit


def test_allow_unsandboxed_is_off_and_not_a_dashboard_setting() -> None:
    from nox.core.config.hands import McpConfig
    from nox.settings.schema import EDITABLE_PATHS

    assert McpConfig().allow_unsandboxed is False
    assert not [path for path in EDITABLE_PATHS if path.startswith("mcp.")]


async def test_a_local_server_without_network_gets_no_egress(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    module = types.SimpleNamespace(EgressDecl=EgressDecl, Limits=Limits)
    launched = await SandboxLauncher(sandbox, module).launch(
        ["srv"], cwd=tmp_path / "w", env={"A": "1"}, network=False
    )
    assert launched.pid == 7 and sandbox.runs[0]["egress"] == EgressDecl()
    assert sandbox.runs[0]["env"] == {"A": "1"}
    assert "cwd" not in sandbox.runs[0]  # the sandbox's own folder, never one under <nox home>
    assert sandbox.runs[0]["workload"] == "mcp:w"  # a sandbox container of its own (N6)
