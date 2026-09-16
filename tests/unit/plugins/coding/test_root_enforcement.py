"""Workspace root enforcement (spec §6.7): `coding.session.start`'s `target` must resolve under one
of the manifest's configured `filesystem_roots` or the call fails before any subprocess exists -
the same "schema validation failure, not an ambiguous confirm" pattern as `obs.scene.switch`'s
`scene_set` check."""

from __future__ import annotations

import os

import pytest
from nox_plugin_coding import create

from .conftest import FakeClient, make_api

pytestmark = pytest.mark.timeout(30)


async def test_target_outside_configured_roots_is_refused_without_spawning(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    api = make_api(fake_client, filesystem_roots=[str(allowed_root)])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "ok"}
    await plugin.start()
    try:
        with pytest.raises(ValueError, match="not under a configured coding workspace root"):
            await api.tools.call(
                "coding.session.start", {"target": str(outside), "prompt": "add a line"}
            )
        assert plugin.runner.sessions == {}
        assert not [n for n, _ in fake_client.events if n == "coding.session_started"]
    finally:
        await plugin.stop()


async def test_target_under_a_configured_root_is_accepted(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    root = tmp_path / "workspace"
    subdir = root / "src"
    subdir.mkdir(parents=True)

    api = make_api(fake_client, filesystem_roots=[str(root)])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    plugin.runner._env = {**os.environ, "FAKE_CLI_MODE": "ok"}
    await plugin.start()
    try:
        result = await api.tools.call(
            "coding.session.start", {"target": str(subdir), "prompt": "add a line"}
        )
        assert result["outcome"] == "ended"
    finally:
        await plugin.stop()


async def test_no_configured_roots_refuses_everything(
    fake_client: FakeClient, fake_cli_command: list[str], tmp_path
) -> None:
    api = make_api(fake_client, filesystem_roots=[])
    plugin = create(api)
    plugin.runner._command_override = fake_cli_command
    await plugin.start()
    try:
        with pytest.raises(ValueError):
            await api.tools.call(
                "coding.session.start", {"target": str(tmp_path), "prompt": "add a line"}
            )
    finally:
        await plugin.stop()
