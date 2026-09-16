"""`security.kill_switch` -> every active session's Claude Code CLI subprocess is actually
terminated (not just marked stopped) and reported as failed - manifest `events.listens:
[security.kill_switch]`, handled exactly like `obs`'s `security.panic` (`api.events.on(...)`, never
raises into the event bus)."""

from __future__ import annotations

import asyncio
import os

import psutil
import pytest
from nox_plugin_coding import create

from .conftest import FakeClient, make_api

pytestmark = pytest.mark.timeout(30)


async def _wait_for_pid(pidfile, timeout: float = 10.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pidfile.exists() and pidfile.read_text().strip():
            return int(pidfile.read_text().strip())
        await asyncio.sleep(0.02)
    raise AssertionError("fake CLI never wrote its pid")


async def test_kill_switch_terminates_the_subprocess_and_reports_failed(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    pidfile = tmp_path / "pid"
    api = make_api(fake_client, filesystem_roots=[str(tmp_path)])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    plugin.runner._env = {
        **os.environ,
        "FAKE_CLI_MODE": "hang",
        "FAKE_CLI_PIDFILE": str(pidfile),
    }
    await plugin.start()
    try:
        task = asyncio.create_task(
            api.tools.call("coding.session.start", {"target": str(tmp_path), "prompt": "hang"})
        )
        pid = await _wait_for_pid(pidfile)
        assert psutil.pid_exists(pid)

        await fake_client.fire("security.kill_switch", {"origin": "hotkey"})
        result = await asyncio.wait_for(task, timeout=10)
        assert result["outcome"] == "cancelled"

        await asyncio.sleep(0.3)
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE

        failed = [p for n, p in fake_client.events if n == "coding.session_failed"]
        assert len(failed) == 1
        assert failed[0]["reason"] == "security.kill_switch engaged"
    finally:
        await plugin.stop()


async def test_kill_switch_with_no_active_sessions_does_not_raise(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api = make_api(fake_client, filesystem_roots=[str(tmp_path)])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    await plugin.start()
    try:
        await fake_client.fire("security.kill_switch", {"origin": "hotkey"})
        assert not [n for n, _ in fake_client.events if n == "coding.session_failed"]
    finally:
        await plugin.stop()


async def test_plugin_stop_also_terminates_a_running_session(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    """`plugin.stop()` (called on `plugin.stop` IPC request and, generically, by the worker on
    `security.kill_switch` too - see `nox.worker.plugin.PluginWorker._on_kill_switch`) must not
    leave an orphaned subprocess behind either."""
    pidfile = tmp_path / "pid"
    api = make_api(fake_client, filesystem_roots=[str(tmp_path)])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    plugin.runner._env = {
        **os.environ,
        "FAKE_CLI_MODE": "hang",
        "FAKE_CLI_PIDFILE": str(pidfile),
    }
    await plugin.start()
    task = asyncio.create_task(
        api.tools.call("coding.session.start", {"target": str(tmp_path), "prompt": "hang"})
    )
    pid = await _wait_for_pid(pidfile)
    await plugin.stop()
    await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
