"""Self-extension, end to end: from "please change this" to a branch someone can read.

A real `NoxCore` in the `coding` profile, the real `coding` plugin worker - with the fake `claude`
in its "writes" mode, so the edit really lands on disk - a real git checkout, the real test
command, and `extend.propose` called the way a model calls it: through the tool executor and the
permission engine. Every confirmation the engine asks for is answered the way the dashboard
answers one, and recorded, so the test also pins down *what* the user is asked.

What it holds in place is the promise in `nox.extend`: the change is on its own branch, the
user's checkout is back where it was, the suite ran against the change, and nothing was merged.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nox.app import PROFILES_DIR, NoxCore
from nox.core.events import E, Event
from nox.plugins.manager import PluginState
from nox.security.model import Decision
from tests.integration.test_coding_plugin import (
    FAKE_CLAUDE_SCRIPT,
    _coding_plugin_dir,
    _config,
    _wait_state,
)

pytestmark = pytest.mark.timeout(180)

#: Passes only when the change the session was asked for is really in the file.
CHECK_THE_CHANGE = (
    "import pathlib, sys; sys.exit(0 if 'line two' in pathlib.Path('notes.txt').read_text() else 1)"
)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return done.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "notes.txt").write_text("line one\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "start")
    return repo


def _writing_fake_claude(tmp_path: Path) -> Path:
    """The fake `claude` in its "writes" mode. The mode is set inside the wrapper: the plugin
    manager hands a plugin only an allow-list of environment variables, so one set on this test
    process never reaches the session."""
    wrapper = tmp_path / "fake_claude_writes.cmd"
    wrapper.write_text(
        f"@echo off\r\nset FAKE_CLI_MODE=writes\r\n"
        f'"{sys.executable}" "{FAKE_CLAUDE_SCRIPT}" %*\r\n',
        encoding="utf-8",
    )
    return wrapper


@pytest.fixture
async def core(tmp_path: Path, checkout: Path):
    plugins_dir = _coding_plugin_dir(
        tmp_path, command=_writing_fake_claude(tmp_path), workspace=checkout
    )
    cfg = _config(tmp_path, plugins_dir=plugins_dir)
    cfg = cfg.model_copy(
        update={
            "extend": cfg.extend.model_copy(
                update={
                    "workspace": str(checkout),
                    "test_command": [sys.executable, "-c", CHECK_THE_CHANGE],
                }
            )
        }
    )
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        await _wait_state(core, "coding", PluginState.RUNNING)
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


def _confirm_like_the_dashboard(core: NoxCore) -> list[dict[str, Any]]:
    """Answer each confirmation "allow", as a user clicking it would; return what was asked."""
    asked: list[dict[str, Any]] = []

    async def on_request(event: Event) -> None:
        payload = dict(event.payload)
        asked.append(payload)
        core.security.engine.reply(payload["request_id"], Decision.ALLOW, by="user")

    core.bus.subscribe(E.SECURITY_PERMISSION_REQUESTED, on_request)
    return asked


async def test_a_proposal_ends_on_its_own_branch_tested_and_unmerged(
    core: NoxCore, checkout: Path
) -> None:
    asked = _confirm_like_the_dashboard(core)

    result = await asyncio.wait_for(
        core.tool_executor.call(
            agent="companion",
            name="extend.propose",
            arguments={"intent": "Add a second line to notes.txt"},
            mode="coding",
        ),
        timeout=120,
    )

    assert result.ok, result.error
    proposal = result.data
    assert proposal["state"] == "ready", proposal
    assert proposal["files"] == [{"change": "M", "path": "notes.txt"}]
    assert proposal["branch"].startswith("nox/proposal/")

    # The user's checkout is exactly where it was: same branch, file untouched, nothing merged.
    assert _git(checkout, "branch", "--show-current") == "main"
    assert (checkout / "notes.txt").read_text(encoding="utf-8") == "line one\n"
    assert _git(checkout, "log", "--format=%s", "main") == "start"

    # The change is on the proposal's branch, as one commit on top of main.
    on_branch = _git(checkout, "show", f"{proposal['branch']}:notes.txt")
    assert on_branch.splitlines() == ["line one", "line two"]
    assert _git(checkout, "rev-list", "--count", f"main..{proposal['branch']}") == "1"

    # The user is asked once, about the proposal itself; the session it starts is part of that
    # answer (profile rule coding.extend.session_for_confirmed_proposal), not a second question.
    assert [(a["tool"], a["action"], a["risk"]) for a in asked] == [("extend", "propose", "high")]


async def test_a_proposal_whose_tests_fail_says_so_and_keeps_the_branch(
    core: NoxCore, checkout: Path
) -> None:
    _confirm_like_the_dashboard(core)
    core.config.extend.test_command[:] = [sys.executable, "-c", "import sys; sys.exit(3)"]

    result = await asyncio.wait_for(
        core.tool_executor.call(
            agent="companion",
            name="extend.propose",
            arguments={"intent": "Add a second line to notes.txt"},
            mode="coding",
        ),
        timeout=120,
    )

    assert result.ok, result.error
    proposal = result.data
    assert proposal["state"] == "tests_failed", proposal
    assert _git(checkout, "branch", "--show-current") == "main"
    assert _git(checkout, "rev-list", "--count", f"main..{proposal['branch']}") == "1"
