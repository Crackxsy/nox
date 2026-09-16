"""Core `clip.*` ToolSpecs (ST-15-01/06): `clip.list`/`clip.tag` read/update the repository,
`clip.export` copies into `export_root` and emits `clip.exported`, `clip.trim` fails safely
without a backend and links parent/child clips when one is available."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.clips.repository import ClipRepository
from nox.clips.tools import (
    make_clip_export_tool,
    make_clip_list_tool,
    make_clip_tag_tool,
    make_clip_trim_tool,
)
from nox.clips.trim import TrimBackend
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


async def test_clip_list_returns_rows(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    spec = make_clip_list_tool(repo)
    result = await spec.handler({"status": None, "limit": 50})
    assert len(result["clips"]) == 1
    db.close()


async def test_clip_tag_updates_and_reports_unknown_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path="a.mp4")
    spec = make_clip_tag_tool(repo)
    result = await spec.handler({"clip_id": row.id, "tags": ["x"], "notes": "n"})
    assert result["clip"]["tags"] == ["x"]
    with pytest.raises(KeyError):
        await spec.handler({"clip_id": "missing", "tags": None, "notes": None})
    db.close()


async def test_clip_export_copies_the_file_and_emits_clip_exported(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    cfg.library_root.mkdir(parents=True)
    src = cfg.library_root / "clip.mp4"
    src.write_bytes(b"clip bytes")
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path=str(src))

    bus = AsyncEventBus()
    exported: list[Event] = []
    bus.subscribe(E.CLIP_EXPORTED, lambda ev: exported.append(ev))
    spec = make_clip_export_tool(repo, cfg, bus)

    result = await spec.handler({"clip_id": row.id})

    assert result["ok"] is True
    dest = Path(result["export_path"])
    assert dest.parent == cfg.export_root
    assert dest.read_bytes() == b"clip bytes"  # noqa: ASYNC240
    assert src.is_file()  # library copy untouched - export never moves the file
    assert repo.get(row.id).status == "exported"
    assert len(exported) == 1
    assert exported[0].payload["export_path"] == str(dest)
    db.close()


async def test_clip_export_fails_cleanly_when_the_file_is_missing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path=str(tmp_path / "gone.mp4"))
    bus = AsyncEventBus()
    spec = make_clip_export_tool(repo, cfg, bus)

    result = await spec.handler({"clip_id": row.id})

    assert result["ok"] is False
    assert result["export_path"] is None
    db.close()


async def test_clip_trim_fails_safely_without_a_backend(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path=str(tmp_path / "a.mp4"))
    backend = TrimBackend(ffmpeg_path=None)
    spec = make_clip_trim_tool(repo, cfg, backend)

    result = await spec.handler({"clip_id": row.id, "in_s": 0.0, "out_s": 5.0})

    assert result["ok"] is False
    assert result["clip_id"] is None
    assert "ffmpeg" in result["reason"]
    assert repo.list_clips() == [row]  # no new row, nothing corrupted
    db.close()


class _FakeBackend:
    """Stands in for `TrimBackend` with a scripted, instant "trim"."""

    def available(self) -> bool:
        return True

    async def trim(self, src: Path, dst: Path, *, in_s: float, out_s: float) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"trimmed")  # noqa: ASYNC240


async def test_clip_trim_success_links_parent_and_child(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    src = tmp_path / "a.mp4"
    src.write_bytes(b"original")
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path=str(src))
    spec = make_clip_trim_tool(repo, cfg, _FakeBackend())  # type: ignore[arg-type]

    result = await spec.handler({"clip_id": row.id, "in_s": 1.0, "out_s": 3.0})

    assert result["ok"] is True
    child = repo.get(result["clip_id"])
    assert child is not None
    assert child.parent_clip_id == row.id
    assert child.duration_s == 2.0
    assert "trim" in child.tags
    assert src.read_bytes() == b"original"  # source clip never modified
    db.close()


async def test_clip_trim_rejects_out_before_in(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = ClipRepository(db)
    cfg = _config(tmp_path)
    row = repo.insert(source="event", trigger_kind="rl.goal", file_path=str(tmp_path / "a.mp4"))
    spec = make_clip_trim_tool(repo, cfg, _FakeBackend())  # type: ignore[arg-type]

    result = await spec.handler({"clip_id": row.id, "in_s": 5.0, "out_s": 1.0})

    assert result["ok"] is False
    db.close()
