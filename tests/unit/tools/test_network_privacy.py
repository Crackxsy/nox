"""Review finding 13: a call that sends data out is a network call for the privacy rules, whatever
its tool name. `shell.run` with a model-supplied `egress` list and `package.*` (a download) used
to pass OFFLINE/PRIVATE because only tool *names* were matched against the cloud patterns.
"""

from __future__ import annotations

import pytest

from nox.core.state import PrivacyMode
from nox.hands.packages import register_package_tools
from nox.hands.shell import ShellSettings, register_shell_tools
from nox.security.audit import SqliteAuditLog
from nox.security.model import Decision
from nox.security.permissions import DefaultPermissionEngine
from nox.tools.executor import ERR_PERMISSION_DENIED, ToolExecutor
from nox.tools.registry import ToolRegistry
from tests.unit.fakes import FakeBus
from tests.unit.tools.conftest import FakeKillSwitch, FakePrivacy


async def _never_runs(argv: list[str]) -> tuple[int, str, str]:
    raise AssertionError("the package runner must not be reached")


@pytest.fixture
def executor(
    registry: ToolRegistry,
    engine: DefaultPermissionEngine,
    audit: SqliteAuditLog,
    bus: FakeBus,
    killswitch: FakeKillSwitch,
) -> ToolExecutor:
    register_shell_tools(
        registry,
        lambda: None,
        lambda: ShellSettings(enabled=True, shell_program=("sh", "-c")),
    )
    register_package_tools(registry, _never_runs)
    return ToolExecutor(registry, engine, audit, bus, killswitch, timeout_s=5.0)


@pytest.mark.parametrize("mode", [PrivacyMode.OFFLINE, PrivacyMode.PRIVATE])
async def test_shell_with_declared_egress_is_refused_offline_and_private(
    executor: ToolExecutor, privacy: FakePrivacy, mode: PrivacyMode
) -> None:
    privacy.mode = mode
    result = await executor.call(
        "companion",
        "shell.run",
        {"command": "curl https://example.com", "egress": ["example.com"]},
        mode="companion",
    )
    assert result.ok is False
    assert result.error == ERR_PERMISSION_DENIED
    assert result.decision == Decision.DENY.value


@pytest.mark.parametrize("mode", [PrivacyMode.OFFLINE, PrivacyMode.PRIVATE])
@pytest.mark.parametrize("tool", ["package.install", "package.remove"])
async def test_packages_are_refused_offline_and_private(
    executor: ToolExecutor, privacy: FakePrivacy, mode: PrivacyMode, tool: str
) -> None:
    privacy.mode = mode
    result = await executor.call("companion", tool, {"package_id": "Git.Git"}, mode="companion")
    assert result.ok is False
    assert result.decision == Decision.DENY.value


def test_executor_marks_egress_and_shows_it_in_the_target(
    registry: ToolRegistry, executor: ToolExecutor
) -> None:
    spec = registry.get("shell.run")
    assert spec is not None and callable(spec.network) and spec.targets is not None
    with_egress = {"command": "x", "egress": ["evil.example"]}
    assert spec.network(with_egress) is True
    assert spec.network({"command": "x", "egress": []}) is False
    assert "egress=evil.example" in spec.targets(with_egress)
