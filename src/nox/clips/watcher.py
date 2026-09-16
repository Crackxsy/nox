"""ClipWatcher (ST-15-04, Spec v0.6 §4.3/§7): periodic scan of `config.clips.watch_dir` (OBS's
replay-buffer output folder) for files not yet indexed - catches clips that appear without a
`clip.requested` round-trip (a manual OBS hotkey save, or promoting a file after the fact). Never
writes to, renames, or deletes anything under `watch_dir` - only reads and copies out, same as
`ClipCaptureService`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

from nox.clips.ingest import ingest_file
from nox.clips.repository import ClipRepository, ClipRow
from nox.core.config import ClipsConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger

log = get_logger(__name__)

VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".flv", ".mov"})


class ClipWatcher:
    def __init__(
        self,
        bus: EventBus,
        repo: ClipRepository,
        config: ClipsConfig,
        *,
        sleep: Callable[[float], asyncio.Future[None]] | None = None,
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._config = config
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.scan_once()
            except OSError as exc:
                log.warning("clips.watcher_scan_failed", error=str(exc))
            await self._sleep(self._config.watch_poll_interval_s)

    async def scan_once(self) -> list[str]:
        """One pass over `watch_dir`; returns the ids of newly-created `clips` rows."""
        watch_dir = self._config.watch_dir
        if not watch_dir.is_dir():
            return []
        candidates = [
            p for p in watch_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES
        ]
        new_ids: list[str] = []
        for path in candidates:
            row = await asyncio.to_thread(self._ingest_if_new, path)
            if row is None:
                continue
            await self._bus.publish(
                Event(
                    name=E.CLIP_SAVED,
                    payload={
                        "clip_id": row.id,
                        "file_path": row.file_path,
                        "trigger_kind": row.trigger_kind,
                        "source": row.source,
                        "duration_s": row.duration_s,
                        "tags": row.tags,
                    },
                )
            )
            new_ids.append(row.id)
        return new_ids

    def _ingest_if_new(self, path: Path) -> ClipRow | None:
        """Blocking: runs off the event loop via `asyncio.to_thread`. Only reads `path` (never
        writes/renames/deletes it) and writes into the library root + the `clips` table."""
        try:
            ingested = ingest_file(path, library_root=self._config.library_root)
        except OSError as exc:
            log.warning("clips.watcher_ingest_failed", path=str(path), error=str(exc))
            return None
        if self._repo.get_by_checksum(ingested.checksum) is not None:
            return None
        return self._repo.insert(
            source="manual",
            trigger_kind="watch_detected",
            file_path=str(ingested.file_path),
            duration_s=ingested.duration_s,
            thumbnail_path=str(ingested.thumbnail_path) if ingested.thumbnail_path else None,
            checksum=ingested.checksum,
        )
