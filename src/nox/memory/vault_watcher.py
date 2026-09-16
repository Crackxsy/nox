"""`watchdog`-based vault watcher with a ~2 s per-file debounce (ST-07-03): a burst of rapid
saves to one note (Obsidian autosave) collapses into exactly one re-index, not N (SP-13).

`watchdog` runs its own OS-thread observer; every callback hops back onto the owning asyncio loop
via `call_soon_threadsafe` so the debounce timers and `VaultIndexer` calls stay single-threaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from nox.core.logging import get_logger
from nox.memory.vault_index import VaultIndexer

log = get_logger(__name__)

DEFAULT_DEBOUNCE_S = 2.0


class VaultWatcher:
    def __init__(
        self,
        indexer: VaultIndexer,
        vault_dir: Path,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._indexer = indexer
        self._vault_dir = Path(vault_dir)
        self._debounce_s = debounce_s
        self._loop = loop
        self._observer: BaseObserver | None = None
        self._pending: dict[str, asyncio.TimerHandle] = {}
        self._reindex_count = 0  # test/observability hook: counts actual index_path() calls

    @property
    def reindex_count(self) -> int:
        return self._reindex_count

    def start(self) -> None:
        if self._observer is not None:
            return
        self._loop = self._loop or asyncio.get_running_loop()
        handler = _Handler(self._on_fs_event)
        observer = Observer()
        observer.schedule(handler, str(self._vault_dir), recursive=True)
        observer.start()
        self._observer = observer
        log.info("memory.vault_watch_started", vault_dir=str(self._vault_dir))

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
        for handle in self._pending.values():
            handle.cancel()
        self._pending.clear()

    # ---- callbacks (called from the watchdog thread) -------------------------------------------

    def _on_fs_event(self, path: str) -> None:
        if not path.endswith(".md"):
            return
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._debounce, path)

    def _debounce(self, path: str) -> None:
        existing = self._pending.get(path)
        if existing is not None:
            existing.cancel()
        assert self._loop is not None
        self._pending[path] = self._loop.call_later(self._debounce_s, self._fire, path)

    def _fire(self, path: str) -> None:
        self._pending.pop(path, None)
        assert self._loop is not None
        self._loop.create_task(self._reindex(Path(path)), name="nox-vault-reindex")

    async def _reindex(self, path: Path) -> None:
        self._reindex_count += 1
        try:
            outcome = await self._indexer.index_path(path)
            log.debug("memory.vault_reindexed", path=str(path), action=outcome.action)
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the watcher
            log.error("memory.vault_reindex_failed", path=str(path), error=str(exc))


class _Handler(FileSystemEventHandler):
    def __init__(self, on_event: Callable[[str], None]) -> None:
        self._on_event = on_event

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._on_event(str(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._on_event(str(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._on_event(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._on_event(str(event.src_path))
            self._on_event(str(event.dest_path))
