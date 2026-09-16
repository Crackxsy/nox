"""Session lifecycle through the plugin's tools (`api.tools.call`, exactly as the core would
dispatch): start -> status.read -> stop, progress events with tool name/files touched, and the
bounded repair loop (max 3 attempts, then stop and report - Personality v1 B.8)."""

from __future__ import annotations

import asyncio
import os

import pytest
from nox_plugin_coding import create

from .conftest import FakeClient, make_api

pytestmark = pytest.mark.timeout(30)


def _plugin(fake_client: FakeClient, fake_cli_command: list[str], workspace, **config):
    api = make_api(
        fake_client,
        filesystem_roots=[str(workspace)],
        **config,
    )
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command  # test-only: bypass shutil.which("claude")
    return api, plugin


async def test_session_start_happy_path_emits_started_then_ended(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
    await plugin.start()
    try:
        plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "ok"}
        result = await api.tools.call(
            "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
        )
        assert result["outcome"] == "ended"
        assert result["repair_attempts"] == 0
        assert result["files_touched"] == ["notes.txt"]
        names = [name for name, _ in fake_client.events]
        assert names[0] == "coding.session_started"
        assert "coding.session_progress" in names
        assert names[-1] == "coding.session_ended"
        progress = [p for n, p in fake_client.events if n == "coding.session_progress"]
        assert any(p["tool_name"] == "Edit" and p["files"] == ["notes.txt"] for p in progress)
    finally:
        await plugin.stop()


async def test_session_status_read_reports_a_tracked_session(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "ok"}
    await plugin.start()
    try:
        started = await api.tools.call(
            "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
        )
        status = await api.tools.call(
            "coding.session.status.read", {"session_id": started["session_id"]}
        )
        assert status["found"] is True
        assert status["outcome"] == "ended"
        missing = await api.tools.call("coding.session.status.read", {"session_id": "nope"})
        assert missing["found"] is False
    finally:
        await plugin.stop()


async def test_session_stop_cancels_and_emits_ended(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    pidfile = tmp_path / "pid"
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
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
        session_id = await _wait_for_session_id(plugin)
        stopped = await api.tools.call(
            "coding.session.stop", {"session_id": session_id, "reason": "user requested"}
        )
        assert stopped["found"] is True
        assert stopped["outcome"] == "cancelled"
        result = await asyncio.wait_for(task, timeout=10)
        assert result["outcome"] == "cancelled"
        assert any(n == "coding.session_ended" for n, _ in fake_client.events)
    finally:
        await plugin.stop()


async def test_repair_loop_stops_after_three_attempts_and_reports(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "always_fail"}
    await plugin.start()
    try:
        result = await api.tools.call(
            "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
        )
        assert result["outcome"] == "failed"
        assert result["repair_attempts"] == 3
        repairing = [
            p
            for n, p in fake_client.events
            if n == "coding.session_progress" and p["stage"] == "repairing"
        ]
        assert len(repairing) == 3
        failed = [p for n, p in fake_client.events if n == "coding.session_failed"]
        assert len(failed) == 1
        assert failed[0]["repair_attempts"] == 3
        assert "repair attempt" in failed[0]["detail"].lower()

        review = await api.tools.call("coding.review.request", {"session_id": result["session_id"]})
        assert "repair attempt" in review["what_is_broken"].lower()
        assert review["next_action"]
    finally:
        await plugin.stop()


async def test_max_turns_failure_is_not_repaired(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "max_turns"}
    await plugin.start()
    try:
        result = await api.tools.call(
            "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
        )
        assert result["outcome"] == "failed"
        assert result["repair_attempts"] == 0
        review = await api.tools.call("coding.review.request", {"session_id": result["session_id"]})
        assert "ran out of turns" in review["what_is_broken"]
    finally:
        await plugin.stop()


async def test_login_failure_is_not_repaired(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api, plugin = _plugin(fake_client, fake_cli_command, tmp_path)
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "logged_out"}
    await plugin.start()
    try:
        result = await api.tools.call(
            "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
        )
        assert result["outcome"] == "failed"
        assert result["repair_attempts"] == 0
    finally:
        await plugin.stop()


async def _wait_for_session_id(plugin, timeout: float = 10.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if plugin.runner.sessions:
            return next(iter(plugin.runner.sessions))
        await asyncio.sleep(0.02)
    raise AssertionError("no session was ever started")
