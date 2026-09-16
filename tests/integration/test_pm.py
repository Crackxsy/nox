"""EPIC-13 PM slice end to end: boots a real headless `NoxCore`, calls `nox.pm.install.install`
against a temp vault with template-shaped fixture notes, and drives `pm.*` tools through the real
`core.tool_executor` (registry -> permission engine -> handler -> audit), exactly as a chat/voice
turn would."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E
from nox.pm.install import PmRuntime, install
from nox.security.model import Decision

pytestmark = pytest.mark.timeout(120)

_EPIC_NOTE = """---
title: "EPIC-13 Test Epic"
type: epic
status: proposed
priority: P1
release: v0.4
owner: nox-implementation
created: 2026-09-09
updated: 2026-09-09
tags: [nox, epic]
machine-data:
  document_id: NOX-EPIC-13
  requirements: []
  stories: []
---

# EPIC-13 Test Epic
"""

_STORY_NOTE = """---
title: "ST-13-01 Todo Story"
type: story
status: todo
epic: EPIC-13
priority: P1
estimate: L
created: 2026-09-09
updated: 2026-09-09
tags: [nox, story, pm]
machine-data:
  document_id: NOX-ST-13-01
  requirements: []
  tests: []
---

# ST-13-01 Todo Story
"""


def _config(tmp_path: Path, vault_dir: Path):
    base = random.randint(20000, 60000)  # noqa: S311
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(vault_dir),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "08 - Epics").mkdir(parents=True)
    (root / "09 - Stories").mkdir(parents=True)
    (root / "08 - Epics" / "EPIC-13 Test Epic.md").write_text(_EPIC_NOTE, encoding="utf-8")
    (root / "09 - Stories" / "ST-13-01 Todo Story.md").write_text(_STORY_NOTE, encoding="utf-8")
    return root


async def installed(core: NoxCore) -> PmRuntime:
    """`install()` returns before the first vault scan has run (it happens off the loop so the
    boot keeps heartbeating); every test here needs the populated index."""
    runtime = install(core)
    assert await runtime.ready(timeout=30.0)
    return runtime


@pytest.fixture
async def core(tmp_path: Path, vault_dir: Path):
    cfg = _config(tmp_path, vault_dir)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        runtime = getattr(core, "pm", None)
        if runtime is not None:
            runtime.stop()  # cancels the background scan and stops the watcher
        await asyncio.wait_for(core.stop(), timeout=30)


async def test_install_registers_pm_tools(core: NoxCore) -> None:
    await installed(core)
    for name in (
        "pm.project.list",
        "pm.epic.list",
        "pm.story.list",
        "pm.story.get",
        "pm.story.create",
        "pm.story.update_status",
        "pm.focus.today",
    ):
        assert name in core.tool_registry


async def test_story_list_runs_through_tool_executor(core: NoxCore) -> None:
    await installed(core)
    result = await core.tool_executor.call("nox.chat", "pm.story.list", {}, mode="companion")
    assert result.ok is True, result.error
    ids = {item["id"] for item in result.data["items"]}
    assert ids == {"ST-13-01"}
    assert result.data["items"][0]["status"] == "todo"


async def test_focus_today_runs_through_tool_executor(core: NoxCore) -> None:
    await installed(core)
    result = await core.tool_executor.call("nox.chat", "pm.focus.today", {}, mode="companion")
    assert result.ok is True, result.error
    assert result.data["items"][0]["id"] == "ST-13-01"


async def test_update_status_confirmed_writes_vault_index_and_project_state(core: NoxCore) -> None:
    await installed(core)

    async def _confirm_when_requested() -> None:
        event = await core.bus.wait_for(E.SECURITY_PERMISSION_REQUESTED)
        core.security.engine.reply(event.payload["request_id"], Decision.ALLOW)

    result, _ = await asyncio.gather(
        core.tool_executor.call(
            "nox.chat",
            "pm.story.update_status",
            {"id": "ST-13-01", "status": "in_progress"},
            mode="companion",
        ),
        _confirm_when_requested(),
    )
    assert result.ok is True, result.error
    assert result.data["item"]["status"] == "in_progress"
    assert core.pm_index.get("ST-13-01").status == "in_progress"
    assert core.state.get("project.active_story") == "ST-13-01"

    on_disk = (core.config.paths.vault_dir / "09 - Stories" / "ST-13-01 Todo Story.md").read_text(
        encoding="utf-8"
    )
    assert "status: in_progress" in on_disk
