"""Ollama helpers for the setup: the pull's own privacy check, progress, and the install offer."""

from __future__ import annotations

from typing import Any

import pytest

from nox.core.state import PrivacyMode
from nox.localai.ollama_setup import (
    PACKAGE_INSTALL_TOOL,
    PullProgress,
    PullRefusedError,
    check_ollama,
    install_instructions,
    pull_model,
    tool_installer,
)
from nox.tools.executor import ToolResult
from tests.unit.localai.fake_ollama import FakeOllama, pull_lines


@pytest.mark.parametrize("mode", [PrivacyMode.OFFLINE, PrivacyMode.PRIVATE])
async def test_pull_model_itself_refuses_before_any_request(mode: PrivacyMode) -> None:
    fake = FakeOllama()

    with pytest.raises(PullRefusedError):
        await pull_model("http://127.0.0.1:11434", "m", fake.factory(), privacy_mode=mode)

    assert fake.requests == []


async def test_progress_sums_every_layer() -> None:
    lines = pull_lines("sha256:a", 1000)[:-1] + pull_lines("sha256:b", 3000)
    fake = FakeOllama(pull=lines)
    fractions: list[float | None] = []

    def seen(progress: PullProgress) -> None:
        fractions.append(progress.fraction)

    result = await pull_model(
        "http://127.0.0.1:11434",
        "m",
        fake.factory(),
        privacy_mode=PrivacyMode.BALANCED,
        on_progress=seen,
    )

    assert result.status == "success"
    assert (result.completed, result.total) == (4000, 4000)
    assert None in fractions and 1.0 in fractions


async def test_check_reports_an_unreachable_server_without_raising() -> None:
    status = await check_ollama(
        "http://127.0.0.1:11434", FakeOllama(reachable=False).factory(), find=lambda _n: None
    )
    assert not status.reachable and not status.installed
    assert "ConnectError" in status.error


async def test_a_different_tag_of_the_same_family_is_not_the_model() -> None:
    status = await check_ollama(
        "http://127.0.0.1:11434", FakeOllama(models=["qwen3:8b"]).factory(), find=lambda _n: None
    )
    assert status.reachable and status.version == "0.12.0"
    assert not status.has_model("qwen3:4b-instruct")
    assert status.has_model("qwen3:8b")


async def test_an_untagged_name_means_latest() -> None:
    status = await check_ollama(
        "http://127.0.0.1:11434", FakeOllama(models=["llama3"]).factory(), find=lambda _n: None
    )
    assert status.has_model("llama3:latest")


@pytest.mark.parametrize(
    ("system", "needle"),
    [("Windows", "winget"), ("Linux", "install.sh"), ("Darwin", "brew")],
)
def test_instructions_per_os(system: str, needle: str) -> None:
    assert any(needle in line for line in install_instructions(system))


class _Registry:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def get(self, name: str) -> object | None:
        return object() if name in self.names else None


class _Executor:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str, dict[str, Any], str]] = []

    async def call(self, agent: str, name: str, arguments: dict[str, Any], *, mode: str) -> Any:
        self.calls.append((agent, name, arguments, mode))
        return ToolResult(ok=self.ok, error=None if self.ok else "permission_denied")


def test_no_installer_without_the_package_tool() -> None:
    assert tool_installer(_Registry(set()), _Executor(True), mode=lambda: "companion") is None
    assert tool_installer(None, None, mode=lambda: "companion") is None


async def test_the_installer_goes_through_the_executor() -> None:
    executor = _Executor(ok=False)
    installer = tool_installer(
        _Registry({PACKAGE_INSTALL_TOOL}), executor, mode=lambda: "companion"
    )
    assert installer is not None

    result = await installer()

    assert executor.calls == [("localai", "package.install", {"package": "ollama"}, "companion")]
    assert not result.ok and result.detail == "permission_denied"
