"""Clip Pipeline, end to end (EPIC-15, Spec v0.6): a real `NoxCore` (headless, no plugin
subprocess - the `rl.event` -> `clip.requested` translation is already covered end-to-end by
`tests/unit/plugins/clips/test_detector.py` against a real `PluginApi`) plus `nox.clips.install.
install(core)`. A `clip.requested` event (what the real, spawned `clips` plugin would have emitted
for a qualifying `rl.event`) is published on the real bus; `ClipCaptureService` calls a fake
`obs.replay_buffer.save` tool (standing in for the `obs` plugin - covered against a real fake OBS
websocket server in `tests/unit/plugins/obs/test_tools.py`) and ingests the resulting file into the
clip library. `ClipWatcher` is also exercised directly against a second fixture file. Modelled on
`tests/integration/test_stream_core.py`, which fakes the twitch plugin's tool the same way."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.clips.install import install
from nox.clips.repository import ClipRepository
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.security.model import Decision, Risk
from nox.tools.registry import ToolSpec
from tests._ports import free_port_base

pytestmark = pytest.mark.timeout(60)


def _config(tmp_path: Path):
    base = free_port_base()
    (tmp_path / "vault").mkdir()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "balanced"},
        "security": {"profile": "stream"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        # No plugin worker is spawned in this test (see module docstring); pin this explicitly so
        # a concurrently-edited `config/defaults.yaml` default (e.g. another epic auto-enabling its
        # own plugin) can never pull in an unrelated subprocess here.
        "plugins": {"enabled": []},
        "clips": {
            "library_root": str(tmp_path / "clips" / "library"),
            "export_root": str(tmp_path / "clips" / "export"),
            "quarantine_root": str(tmp_path / "clips" / "quarantine"),
            "watch_dir": str(tmp_path / "clips" / "incoming"),
            "cooldown_s": 0.01,
            "watch_poll_interval_s": 3600.0,  # the watcher is driven manually (`scan_once`) here
        },
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


class _ReplayBufferSaveInput(BaseModel):
    reason: str = ""


def _register_fake_obs_replay_buffer_save(core: NoxCore, saved_file: Path) -> list[dict[str, Any]]:
    """Stands in for the `obs` plugin's `obs.replay_buffer.save` tool, same pattern as
    `tests/integration/test_stream_core.py`'s fake `twitch.chat.send`."""
    calls: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"ok": True, "file_path": str(saved_file), "reason": ""}

    core.tool_registry.register(
        ToolSpec(
            name="obs.replay_buffer.save",
            description="test double for the obs plugin's replay-buffer save tool",
            input_model=_ReplayBufferSaveInput,
            risk=Risk.MEDIUM,
            handler=handler,
        )
    )
    return calls


async def _publish_and_auto_confirm(core: NoxCore, event: Event, *, timeout: float = 15.0) -> None:
    """`obs.replay_buffer.save` is `medium` risk; under the `stream` profile that means CONFIRM
    (Spec v0.6 §9 leaves the "pre-approved event kinds, confirm: no" allow-list to profile config,
    which this test does not own/edit). Publish in the background and answer the pending grant like
    a real confirm UI would, instead of either hanging on the engine's 60 s confirm timeout or
    weakening the fake tool's risk level."""
    task = asyncio.ensure_future(core.bus.publish(event))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done() and loop.time() < deadline:
        for grant_id in core.security.engine.pending():
            core.security.engine.reply(grant_id, Decision.ALLOW, remember=True, by="test")
        await asyncio.sleep(0.02)
    await asyncio.wait_for(task, timeout=5.0)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


async def test_clip_requested_produces_a_saved_clip_row_end_to_end(
    core: NoxCore, tmp_path: Path
) -> None:
    # The file OBS's replay buffer "wrote" - the fake tool below resolves this path, exactly like
    # `nox_plugin_obs.ObsPlugin.replay_buffer_save` resolves a real `ReplayBufferSaved` event.
    saved_file = tmp_path / "obs_output" / "replay_goal.mp4"
    saved_file.parent.mkdir(parents=True, exist_ok=True)
    saved_file.write_bytes(b"fake replay buffer bytes")
    calls = _register_fake_obs_replay_buffer_save(core, saved_file)

    runtime = install(core)
    try:
        saved_events: list[Event] = []
        core.bus.subscribe(E.CLIP_SAVED, lambda ev: saved_events.append(ev))

        # What the real `clips` plugin's detector emits for a qualifying `rl.event` (Spec v0.6
        # §4.1) - the translation itself is covered by `tests/unit/plugins/clips/test_detector.py`.
        await _publish_and_auto_confirm(
            core,
            Event(
                name=E.CLIP_REQUESTED,
                payload={
                    "trigger_kind": "rl.goal",
                    "source": "event",
                    "origin_event_id": "",
                    "session_id": "s1",
                    "tags": [],
                },
            ),
        )

        assert len(saved_events) == 1
        assert saved_events[0].payload["trigger_kind"] == "rl.goal"
        assert calls and calls[0]["reason"] == "rl.goal"

        repo = ClipRepository(core.db)
        rows = repo.list_clips()
        assert len(rows) == 1
        assert rows[0].status == "new"
        assert rows[0].session_id == "s1"
        assert Path(rows[0].file_path).is_file()  # noqa: ASYNC240
        # copied into the library, not referenced in place; OBS's own output stays untouched
        assert Path(rows[0].file_path) != saved_file
        assert saved_file.read_bytes() == b"fake replay buffer bytes"

        # ClipWatcher: a second file that shows up in the watch folder without a clip.requested
        # round-trip (e.g. a manual OBS hotkey save) is still indexed, and the source is untouched.
        watched_file = core.config.clips.watch_dir / "manual_save.mp4"
        watched_file.parent.mkdir(parents=True, exist_ok=True)
        watched_file.write_bytes(b"manually saved bytes")
        new_ids = await runtime.watcher.scan_once()

        assert len(new_ids) == 1
        watched_row = repo.get(new_ids[0])
        assert watched_row is not None
        assert watched_row.trigger_kind == "watch_detected"
        assert watched_file.read_bytes() == b"manually saved bytes"
        assert len(repo.list_clips()) == 2
    finally:
        await runtime.stop()
