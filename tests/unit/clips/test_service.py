"""`ClipCaptureService` (ST-15-02/03): `clip.requested` -> `obs.replay_buffer.save` (via a fake
`ToolExecutor`) -> file copied into the library -> `clips` row -> `clip.saved`/`clip.failed`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.clips.repository import ClipRepository
from nox.clips.service import ClipCaptureService
from nox.core.bus import AsyncEventBus
from nox.core.config import ClipsConfig
from nox.core.events import E, Event
from nox.data.db import Database
from nox.tools.executor import ToolResult


class FakeExecutor:
    """Records every call and returns a scripted `ToolResult` (default: `obs.replay_buffer.save`
    succeeds with a fixed file path)."""

    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    async def call(
        self, *, agent: str, name: str, arguments: dict[str, Any], mode: str, **_: Any
    ) -> ToolResult:
        self.calls.append({"agent": agent, "name": name, "arguments": arguments, "mode": mode})
        if self.result is not None:
            return self.result
        return ToolResult(ok=True, data={"ok": True, "file_path": None, "reason": ""})


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repo(db: Database) -> ClipRepository:
    return ClipRepository(db)


def _config(tmp_path: Path, **overrides: Any) -> ClipsConfig:
    return ClipsConfig(
        library_root=tmp_path / "library",
        export_root=tmp_path / "export",
        quarantine_root=tmp_path / "quarantine",
        watch_dir=tmp_path / "incoming",
        **overrides,
    )


def _source_file(tmp_path: Path, name: str = "replay.mp4") -> Path:
    src = tmp_path / "incoming" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"fake video bytes")
    return src


async def test_successful_capture_creates_a_clip_row_and_emits_clip_saved(
    tmp_path: Path, repo: ClipRepository
) -> None:
    bus = AsyncEventBus()
    src = _source_file(tmp_path)
    executor = FakeExecutor(
        ToolResult(ok=True, data={"ok": True, "file_path": str(src), "reason": ""})
    )
    service = ClipCaptureService(bus, executor, repo, _config(tmp_path))
    service.start()

    saved: list[Event] = []
    bus.subscribe(E.CLIP_SAVED, lambda ev: saved.append(ev))

    await bus.publish(
        Event(
            name=E.CLIP_REQUESTED,
            payload={"trigger_kind": "rl.goal", "source": "event", "session_id": "s1", "tags": []},
        )
    )

    assert len(saved) == 1
    assert saved[0].payload["trigger_kind"] == "rl.goal"
    rows = repo.list_clips()
    assert len(rows) == 1
    assert Path(rows[0].file_path).is_file()  # noqa: ASYNC240
    assert rows[0].file_path != str(src)  # copied, not referenced in place
    assert src.is_file()  # the watch-dir source file is never touched
    assert executor.calls[0]["name"] == "obs.replay_buffer.save"


async def test_replay_buffer_disabled_emits_clip_failed_and_no_row(
    tmp_path: Path, repo: ClipRepository
) -> None:
    bus = AsyncEventBus()
    executor = FakeExecutor(
        ToolResult(
            ok=True, data={"ok": False, "file_path": None, "reason": "replay buffer is not active"}
        )
    )
    service = ClipCaptureService(bus, executor, repo, _config(tmp_path))
    service.start()

    failed: list[Event] = []
    bus.subscribe(E.CLIP_FAILED, lambda ev: failed.append(ev))

    await bus.publish(
        Event(name=E.CLIP_REQUESTED, payload={"trigger_kind": "rl.goal", "source": "event"})
    )

    assert len(failed) == 1
    assert "replay buffer" in failed[0].payload["reason"]
    assert repo.list_clips() == []


async def test_permission_denied_emits_clip_failed(tmp_path: Path, repo: ClipRepository) -> None:
    bus = AsyncEventBus()
    executor = FakeExecutor(ToolResult(ok=False, error="permission.denied"))
    service = ClipCaptureService(bus, executor, repo, _config(tmp_path))
    service.start()

    failed: list[Event] = []
    bus.subscribe(E.CLIP_FAILED, lambda ev: failed.append(ev))

    await bus.publish(
        Event(name=E.CLIP_REQUESTED, payload={"trigger_kind": "rl.goal", "source": "event"})
    )

    assert len(failed) == 1
    assert repo.list_clips() == []


async def test_cooldown_skips_a_second_request_for_the_same_kind(
    tmp_path: Path, repo: ClipRepository
) -> None:
    bus = AsyncEventBus()
    src = _source_file(tmp_path)
    executor = FakeExecutor(
        ToolResult(ok=True, data={"ok": True, "file_path": str(src), "reason": ""})
    )
    service = ClipCaptureService(bus, executor, repo, _config(tmp_path, cooldown_s=1000.0))
    service.start()

    await bus.publish(
        Event(name=E.CLIP_REQUESTED, payload={"trigger_kind": "rl.goal", "source": "event"})
    )
    await bus.publish(
        Event(name=E.CLIP_REQUESTED, payload={"trigger_kind": "rl.goal", "source": "event"})
    )

    assert len(executor.calls) == 1
    assert len(repo.list_clips()) == 1
