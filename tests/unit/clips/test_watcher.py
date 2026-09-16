"""`ClipWatcher` (ST-15-04): picks up files in the watch folder that never went through
`clip.requested`, indexes them with checksum/thumbnail (opencv optional), and never writes to,
renames or deletes anything under `watch_dir`."""

from __future__ import annotations

from pathlib import Path

from nox.clips.repository import ClipRepository
from nox.clips.watcher import ClipWatcher
from nox.core.bus import AsyncEventBus
from nox.core.config import ClipsConfig
from nox.core.events import E, Event
from nox.data.db import Database


def _db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.migrate()
    return database


def _config(tmp_path: Path) -> ClipsConfig:
    return ClipsConfig(
        library_root=tmp_path / "library",
        export_root=tmp_path / "export",
        quarantine_root=tmp_path / "quarantine",
        watch_dir=tmp_path / "incoming",
    )


async def test_scan_once_indexes_a_new_file(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    cfg.watch_dir.mkdir(parents=True)
    src = cfg.watch_dir / "replay_001.mp4"
    src.write_bytes(b"video bytes")

    bus = AsyncEventBus()
    saved: list[Event] = []
    bus.subscribe(E.CLIP_SAVED, lambda ev: saved.append(ev))
    watcher = ClipWatcher(bus, repo, cfg)

    new_ids = await watcher.scan_once()

    assert len(new_ids) == 1
    assert len(saved) == 1
    rows = repo.list_clips()
    assert len(rows) == 1
    assert rows[0].source == "manual"
    assert rows[0].trigger_kind == "watch_detected"
    assert Path(rows[0].file_path).read_bytes() == b"video bytes"  # noqa: ASYNC240
    assert src.is_file() and src.read_bytes() == b"video bytes"  # source untouched
    db.close()


async def test_scan_once_does_not_duplicate_an_already_indexed_file(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    cfg.watch_dir.mkdir(parents=True)
    src = cfg.watch_dir / "replay_001.mp4"
    src.write_bytes(b"same bytes")

    bus = AsyncEventBus()
    watcher = ClipWatcher(bus, repo, cfg)

    first = await watcher.scan_once()
    second = await watcher.scan_once()

    assert len(first) == 1
    assert second == []
    assert len(repo.list_clips()) == 1
    db.close()


async def test_scan_once_ignores_non_video_files(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    cfg.watch_dir.mkdir(parents=True)
    (cfg.watch_dir / "notes.txt").write_text("hi")

    bus = AsyncEventBus()
    watcher = ClipWatcher(bus, repo, cfg)

    assert await watcher.scan_once() == []
    assert repo.list_clips() == []
    db.close()


async def test_scan_once_on_missing_watch_dir_is_a_noop(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)  # watch_dir never created

    bus = AsyncEventBus()
    watcher = ClipWatcher(bus, repo, cfg)

    assert await watcher.scan_once() == []
    db.close()
