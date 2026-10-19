"""The `ai.setup.*` requests: who may call them, a background run the page polls, and refusals."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from nox.core.state import PrivacyMode
from nox.ipc.dispatch import EmptyPayload, RequestRegistry
from nox.localai import install as localai_install
from nox.localai.ipc import LocalAiService, SetupStart, register_localai_ipc
from nox.localai.setup import SetupDeps
from tests.unit.localai.fake_ollama import FakeOllama
from tests.unit.localai.test_setup import _deps

NAMES = ("ai.setup.status", "ai.setup.check", "ai.setup.start")
CTX: Any = SimpleNamespace(role="dashboard", client_id="dashboard:test")


def _registry(deps: SetupDeps) -> tuple[RequestRegistry, LocalAiService]:
    registry = RequestRegistry()
    service = LocalAiService(lambda: deps)
    register_localai_ipc(registry, service)
    return registry, service


async def _call(registry: RequestRegistry, name: str, payload: Any = None) -> dict[str, Any]:
    registration = registry.get(name)
    assert registration is not None
    result = await registration.handler(CTX, payload or EmptyPayload())
    assert isinstance(result, dict)
    return result


async def _finished(service: LocalAiService) -> None:
    for _ in range(200):
        if not service.running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the setup run did not finish")


def test_only_the_shell_and_the_dashboard_may_call_them(tmp_path: Path) -> None:
    registry, _ = _registry(_deps(FakeOllama(), tmp_path))
    for name in NAMES:
        registration = registry.get(name)
        assert registration is not None
        assert registration.allowed_roles == frozenset({"shell", "dashboard"})


async def test_status_before_any_run_is_idle(tmp_path: Path) -> None:
    registry, _ = _registry(_deps(FakeOllama(), tmp_path))
    status = await _call(registry, "ai.setup.status")
    assert status["phase"] == "idle" and status["outcome"] is None and not status["running"]


async def test_check_is_a_dry_run(tmp_path: Path) -> None:
    fake = FakeOllama()
    registry, _ = _registry(_deps(fake, tmp_path))

    status = await _call(registry, "ai.setup.check")

    assert status["outcome"] == "recommended"
    assert status["recommendation"]["model"] == "qwen3:4b-instruct"
    assert status["ollama"]["reachable"] is True
    assert "/api/pull" not in fake.paths()


async def test_start_runs_in_the_background_and_the_page_polls_it(tmp_path: Path) -> None:
    fake = FakeOllama()
    registry, service = _registry(_deps(fake, tmp_path))

    reply = await _call(registry, "ai.setup.start", SetupStart())
    assert reply["ok"] is True and reply["running"] is True
    again = await _call(registry, "ai.setup.start", SetupStart())
    assert again["ok"] is False and again["reason"] == "running"

    await _finished(service)
    status = await _call(registry, "ai.setup.status")
    assert status["outcome"] == "done", status
    assert status["progress"]["completed"] == status["progress"]["total"] == 1000
    assert status["model_written"] == "qwen3:4b-instruct"
    written = yaml.safe_load((tmp_path / "user.yaml").read_text("utf-8"))
    assert written["ai"]["providers"]["ollama"]["model"] == "qwen3:4b-instruct"
    assert len(fake.pull_bodies) == 1


async def test_a_dashboard_run_in_offline_mode_downloads_nothing(tmp_path: Path) -> None:
    fake = FakeOllama()
    registry, service = _registry(_deps(fake, tmp_path, privacy=PrivacyMode.OFFLINE))

    await _call(registry, "ai.setup.start", SetupStart())
    await _finished(service)

    status = await _call(registry, "ai.setup.status")
    assert status["outcome"] == "privacy_blocked"
    assert "/api/pull" not in fake.paths()
    assert not (tmp_path / "user.yaml").exists()


async def test_ollama_is_installed_only_when_the_page_asked_for_it(tmp_path: Path) -> None:
    installs: list[str] = []

    async def installer() -> Any:
        installs.append("x")
        return SimpleNamespace(ok=False, detail="permission_denied")

    deps = _deps(FakeOllama(reachable=False), tmp_path, installer=installer)
    registry, service = _registry(deps)

    await _call(registry, "ai.setup.start", SetupStart())
    await _finished(service)
    assert (await _call(registry, "ai.setup.status"))["outcome"] == "ollama_missing"
    assert installs == []

    await _call(registry, "ai.setup.start", SetupStart(install_ollama=True))
    await _finished(service)
    assert (await _call(registry, "ai.setup.status"))["outcome"] == "install_failed"
    assert installs == ["x"]


def test_install_registers_the_requests_on_the_core() -> None:
    registry = RequestRegistry()
    core: Any = SimpleNamespace(
        config=SimpleNamespace(ai=SimpleNamespace(model_dump=dict)),
        security=SimpleNamespace(privacy=SimpleNamespace(mode=PrivacyMode.BALANCED)),
        state=SimpleNamespace(get=lambda _key: "companion"),
        registry=registry,
        tool_registry=None,
        tool_executor=None,
    )
    service = localai_install.install(core)
    assert isinstance(service, LocalAiService)
    assert all(registry.get(name) is not None for name in NAMES)
