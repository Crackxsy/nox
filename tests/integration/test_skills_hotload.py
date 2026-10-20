"""Self-built skills end to end on a real headless core: learn (fake builder), check, test,
activate - the real plugin worker starts from `<nox home>/plugins` without a restart, its tool
asks before it runs, and deactivating removes it again."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, REPO_ROOT, NoxCore
from nox.core.config import load_config
from nox.plugins.manager import ORIGIN_SELF_BUILT, PluginState
from nox.security.model import Decision
from nox.skills.service import SkillState
from nox.skills.worker_launch import SandboxedWorker
from tests._fake_skill_builder import FakeSkillBuilder
from tests._ports import free_port_base

pytestmark = pytest.mark.timeout(240)


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    plugins = tmp_path / "bundled"
    plugins.mkdir()
    shutil.copytree(REPO_ROOT / "plugins" / "echo", plugins / "echo")
    overrides = {
        "paths": {
            name: str(tmp_path / name.removesuffix("_dir"))
            for name in (
                "data_dir",
                "vault_dir",
                "index_dir",
                "database_dir",
                "cache_dir",
                "backups_dir",
                "runtime_dir",
                "logs_dir",
            )
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "plugins": {"enabled": [], "dir": str(plugins)},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):  # type: ignore[no-untyped-def]
    core = NoxCore(_config(tmp_path), voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=90)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def _until(predicate, timeout: float = 60) -> None:  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.1)


async def test_a_skill_is_learned_activated_used_and_removed_without_a_restart(
    core: NoxCore,
) -> None:
    service = core.extensions["skills"].service
    if core.plugins.sandboxed_spawner.unavailable() is not None:
        pytest.skip("no strong sandbox on this machine: skills cannot run here (fail closed)")
    service.builder = FakeSkillBuilder()
    assert (await service.learn("Wuerfeln", capability="dice", by="companion"))["ok"]
    await service.wait()
    record = service.get("dice")
    assert record is not None and record.state is SkillState.READY, record
    assert core.tool_registry.get("dice.roll") is None

    assert (await service.activate("dice", pin=None, by="dashboard"))["ok"]
    try:
        await _until(lambda: core.plugins.state_of("dice") is PluginState.RUNNING)
    except AssertionError:
        # Why the worker did not come up: the manager's reason and the worker's own stderr.
        rec = core.plugins.records().get("dice")
        tail = getattr(getattr(rec, "process", None), "stderr_tail", lambda: "")()
        pytest.fail(f"the skill never ran: {rec.reason if rec else 'no record'}\n{tail[-3000:]}")
    assert core.plugins.records()["dice"].origin == ORIGIN_SELF_BUILT
    # The worker is the sandboxed relay, never a user-rights `python -m nox.worker` process.
    assert isinstance(core.plugins.records()["dice"].process, SandboxedWorker)
    assert "dice" in core.config.plugins.enabled

    engine = core.security.engine
    call = asyncio.create_task(
        core.tool_executor.call("companion", "dice.roll", {"sides": 6}, mode="companion")
    )
    await _until(lambda: bool(engine.pending()), timeout=10)  # a self-built tool asks first
    engine.reply(engine.pending()[0], Decision.ALLOW)
    result = await asyncio.wait_for(call, timeout=30)
    assert result.ok and result.data is not None and 1 <= result.data["value"] <= 6

    assert (await service.deactivate("dice", by="dashboard"))["ok"]
    assert core.tool_registry.get("dice.roll") is None
    assert "dice" not in core.config.plugins.enabled
    assert (await service.forget("dice", by="dashboard"))["ok"]
    assert "dice" not in core.plugins.records()
