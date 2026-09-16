"""nox.core.statemgr: dotted updates, versioning, state.changed, checkpoints, restore."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event
from nox.core.state import Mode, NoxState
from nox.core.statemgr import NoxStateManager, StatePathError
from nox.data.db import Database
from nox.data.repos import StateCheckpointRepository


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> StateCheckpointRepository:
    return StateCheckpointRepository(db)


async def test_update_bumps_version_and_emits_state_changed(
    repo: StateCheckpointRepository,
) -> None:
    bus = AsyncEventBus()
    events: list[Event] = []
    bus.subscribe(E.STATE_CHANGED, events.append)
    sm = NoxStateManager(bus, repo, checkpoint_interval_s=10)
    v1 = await sm.update("assistant.mood.energy", 0.9)
    v2 = await sm.update("user.activity", "coding", reason="sensor")
    assert (v1, v2) == (1, 2)
    assert sm.state.version == 2
    assert sm.get("assistant.mood.energy") == 0.9
    assert sm.get("user").activity == "coding"
    assert events[0].payload == {
        "path": "assistant.mood.energy",
        "old": 0.6,
        "new": 0.9,
        "version": 1,
    }
    assert events[1].payload["new"] == "coding"
    await sm.close()


async def test_enum_and_dict_paths_and_json_snapshot(repo: StateCheckpointRepository) -> None:
    sm = NoxStateManager(AsyncEventBus(), repo, checkpoint_interval_s=10)
    await sm.update("assistant.mode", "coding")
    assert sm.get("assistant.mode") is Mode.CODING
    await sm.update("user.mood_estimate.frustration", 0.7)
    assert sm.get("user.mood_estimate") == {"frustration": 0.7}
    await sm.update("assistant.mood", {"energy": 0.1})
    assert sm.get("assistant.mood.energy") == 0.1
    snap = sm.snapshot()
    json.dumps(snap)
    assert snap["assistant"]["mode"] == "coding"
    await sm.close()


async def test_invalid_values_and_paths_rejected(repo: StateCheckpointRepository) -> None:
    sm = NoxStateManager(AsyncEventBus(), repo, checkpoint_interval_s=10)
    with pytest.raises(ValueError, match="assistant.mode"):
        await sm.update("assistant.mode", "not-a-mode")
    with pytest.raises(ValueError, match="assistant.mood.energy"):
        await sm.update("assistant.mood.energy", "high")
    with pytest.raises(StatePathError):
        await sm.update("assistant.nope", 1)
    with pytest.raises(StatePathError):
        sm.get("system.missing")
    with pytest.raises(StatePathError):
        await sm.update("version", 99)
    assert sm.state.version == 0  # nothing applied
    await sm.close()


async def test_immediate_checkpoint_for_important_paths(repo: StateCheckpointRepository) -> None:
    sm = NoxStateManager(AsyncEventBus(), repo, checkpoint_interval_s=10)
    await sm.update("privacy.mode", "private")
    latest = repo.latest()
    assert latest is not None and latest.version == 1
    await sm.update("system.level", "safe_mode")
    assert repo.latest().version == 2  # type: ignore[union-attr]
    await sm.update("voice.listening", True, reason="kill switch")
    assert repo.latest().version == 3  # type: ignore[union-attr]
    await sm.close()


async def test_scheduled_checkpoint_is_debounced(repo: StateCheckpointRepository) -> None:
    sm = NoxStateManager(AsyncEventBus(), repo, checkpoint_interval_s=0.15)
    await sm.update("voice.listening", True)
    await sm.update("voice.speaking", True)
    await asyncio.sleep(0.05)
    assert repo.latest() is None
    await asyncio.sleep(0.2)
    latest = repo.latest()
    assert latest is not None and latest.version == 2 and repo.count() == 1
    await sm.close()
    assert repo.count() == 1  # nothing new to flush


async def test_restore_latest_and_schema_guard(repo: StateCheckpointRepository) -> None:
    sm = NoxStateManager(AsyncEventBus(), repo, checkpoint_interval_s=10)
    assert await sm.restore_latest() is False
    await sm.update("assistant.mode", "research")
    await sm.update("user.activity", "playing")
    await sm.flush()
    fresh = NoxStateManager(AsyncEventBus(), repo)
    assert await fresh.restore_latest() is True
    assert fresh.state.version == 2
    assert fresh.get("assistant.mode") is Mode.RESEARCH
    assert fresh.get("user.activity") == "playing"
    await fresh.update("voice.listening", True)
    assert fresh.state.version == 3  # continues from the restored version

    repo.save(99, "corrupt", "{not json")
    broken = NoxStateManager(AsyncEventBus(), repo)
    assert await broken.restore_latest() is False
    other = NoxState(schema_version=2)
    repo.save(5, "schema", other.model_dump_json())
    assert await NoxStateManager(AsyncEventBus(), repo).restore_latest() is False


async def test_without_repository_everything_still_works() -> None:
    sm = NoxStateManager(AsyncEventBus())
    await sm.update("assistant.mode", "focus", reason="mode")
    await sm.checkpoint(immediate=True)
    assert await sm.restore_latest() is False
    await sm.close()
