"""`session_timeout_s`: the manifest promised it, and nothing enforced it.

A session past its limit has its Claude Code CLI stopped - the process really gone - and ends as a
failure that names the limit, never as one left running unattended or reported as cancelled by the
user.
"""

from __future__ import annotations

import asyncio
import os

import psutil
import pytest
from nox_plugin_coding import create

from .conftest import FakeClient, make_api
from .test_kill_switch import _wait_for_pid

pytestmark = pytest.mark.timeout(30)


def test_the_limit_comes_from_the_manifest(fake_client: FakeClient, tmp_path) -> None:
    api = make_api(fake_client, filesystem_roots=[str(tmp_path)], session_timeout_s=42)
    assert create(api).runner.session_timeout_s == 42


async def test_a_session_past_its_limit_is_stopped_and_fails(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    pidfile = tmp_path / "pid"
    api = make_api(fake_client, filesystem_roots=[str(tmp_path)], session_timeout_s=1.0)
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "hang", "FAKE_CLI_PIDFILE": str(pidfile)}
    await plugin.start()
    try:
        task = asyncio.create_task(
            api.tools.call("coding.session.start", {"target": str(tmp_path), "prompt": "hang"})
        )
        pid = await _wait_for_pid(pidfile)

        result = await asyncio.wait_for(task, timeout=10)

        assert result["outcome"] == "failed"
        assert "longer than 1 s" in result["last_error"]
        await asyncio.sleep(0.3)
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        failed = [p for n, p in fake_client.events if n == "coding.session_failed"]
        assert len(failed) == 1
    finally:
        await plugin.stop()


def test_a_session_reads_the_workspaces_settings_and_not_the_persons_own() -> None:
    """Unattended means deterministic: personal hooks and permissions once refused every edit."""
    from nox_plugin_coding.session import build_session_args

    args = build_session_args(
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit"],
        add_dir="workspace",
        max_turns=5,
        resume=None,
        model="sonnet",
    )
    assert args[args.index("--setting-sources") + 1] == "project"
