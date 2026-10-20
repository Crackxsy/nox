"""A self-built skill's worker is started only through the sandboxed spawner, never as a process."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nox.plugins.manager import ORIGIN_SELF_BUILT, PluginState

from .conftest import FakeProcess, write_manifest
from .test_lifecycle import build, wait_until


class Spawner:
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self.calls: list[dict[str, Any]] = []

    def unavailable(self) -> str | None:
        return self.reason

    async def spawn(self, **kwargs: Any) -> FakeProcess:
        self.calls.append(kwargs)
        return FakeProcess(pid=777)


def self_built(tmp_path: Path, plugins_dir: Path, spawner: Spawner | None) -> Any:
    skills = tmp_path / "self_built"
    write_manifest(
        skills,
        "dice",
        entry="nox_skill_dice:create",
        permissions=[{"tool": "dice.roll", "risk": "medium"}],
        events={"emits": [], "listens": []},
    )
    harness = build(plugins_dir, enabled=("dice",))
    harness.manager.extra_dirs = {ORIGIN_SELF_BUILT: skills}
    harness.manager.sandboxed_spawner = spawner
    return harness, skills


async def test_without_a_spawner_a_skill_never_runs(tmp_path: Path, plugins_dir: Path) -> None:
    harness, _ = self_built(tmp_path, plugins_dir, None)
    await harness.manager.start()
    record = harness.manager.records()["dice"]
    assert record.origin == ORIGIN_SELF_BUILT
    assert record.state is PluginState.FAILED and "sandbox" in record.reason
    assert harness.processes == []  # no user-rights `python -m nox.worker` was started
    await harness.manager.stop("test")


async def test_without_a_strong_sandbox_a_skill_never_runs(
    tmp_path: Path, plugins_dir: Path
) -> None:
    spawner = Spawner(reason="no sandbox here")
    harness, _ = self_built(tmp_path, plugins_dir, spawner)
    await harness.manager.start()
    record = harness.manager.records()["dice"]
    assert record.state is PluginState.FAILED and "no sandbox here" in record.reason
    assert harness.processes == [] and spawner.calls == []
    await harness.manager.stop("test")


async def test_a_skill_runs_through_the_sandboxed_spawner(
    tmp_path: Path, plugins_dir: Path
) -> None:
    spawner = Spawner()
    harness, skills = self_built(tmp_path, plugins_dir, spawner)
    write_manifest(plugins_dir, "demo")
    harness.manager.settings.enabled.append("demo")
    await harness.manager.start()
    await wait_until(lambda: harness.manager.state_of("dice") is PluginState.SPAWNED)
    call = spawner.calls[0]
    assert call["plugin_id"] == "dice" and call["plugin_dir"] == skills / "dice"
    assert call["token"] and call["hub_url"].startswith("ws://")
    assert harness.manager.records()["dice"].process.pid == 777  # type: ignore[union-attr]
    # The bundled plugin next to it still starts the ordinary way, and only it.
    assert len(harness.processes) == 1
    await harness.manager.stop("test")


async def test_a_host_approved_for_one_skill_is_not_approved_for_another(
    tmp_path: Path, plugins_dir: Path
) -> None:
    skills = tmp_path / "self_built"
    for sid in ("alpha", "beta"):
        write_manifest(
            skills,
            sid,
            entry=f"nox_skill_{sid}:create",
            permissions=[{"tool": f"{sid}.get", "risk": "medium"}],
            events={"emits": [], "listens": []},
            network={"egress": ["api.example.org:443"]},
        )
    harness = build(plugins_dir, enabled=("alpha", "beta"))
    manager = harness.manager
    manager.extra_dirs = {ORIGIN_SELF_BUILT: skills}
    manager.sandboxed_spawner = Spawner()
    manager.allow_egress("alpha", ["api.example.org:443"])
    await manager.start()
    await wait_until(lambda: manager.state_of("alpha") is PluginState.SPAWNED)
    beta = manager.records()["beta"]
    assert beta.state is PluginState.FAILED and "api.example.org" in beta.reason
    assert manager.global_egress_allowlist == ()  # nothing leaked into the global list
    await manager.stop("test")
    manager.revoke_egress("alpha")
    await manager.rescan()
    assert manager.records()["alpha"].state is PluginState.FAILED
