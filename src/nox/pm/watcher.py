"""Vault watcher: watches the configured epics/stories folders with `watchdog`, debounces bursty
saves
into one reindex pass, and emits `pm.item_changed` only for notes whose `note_hash` actually
changed (idempotent re-index) plus `pm.focus_changed` when the day's ranked focus list changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.pm.focus import FocusService
from nox.pm.index import PmIndex
from nox.pm.models import WorkItem
from nox.pm.vault_repo import PmVaultRepo

log = get_logger(__name__)


class _Handler(FileSystemEventHandler):
    """Forwards every `.md` create/modify/delete/move under a watched folder to `on_change`."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def _maybe(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if path.endswith(".md"):
            self._on_change()

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._maybe(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe(event)


class PmWatcher:
    """Runs a `watchdog.Observer` thread over the repo's watched folders; every filesystem event
    schedules (or reschedules) one debounced reindex on the asyncio loop, so a burst of saves
    triggers exactly one `reindex_once` call."""

    def __init__(
        self,
        repo: PmVaultRepo,
        index: PmIndex,
        bus: EventBus,
        *,
        focus: FocusService | None = None,
        debounce_s: float = 1.5,
    ) -> None:
        self._repo = repo
        self._index = index
        self._bus = bus
        self._focus = focus
        self._debounce_s = debounce_s
        self._observer: Any | None = None  # watchdog.observers.Observer has no usable type stub
        self._loop: asyncio.AbstractEventLoop | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._known_hashes: dict[str, str] = {}
        self._reindex_count = 0

    @property
    def reindex_count(self) -> int:
        """Number of completed `reindex_once` runs - test/observability hook."""
        return self._reindex_count

    def seed_known_hashes(self, items: list[WorkItem]) -> None:
        """Prime the change-detection baseline (e.g. after an initial synchronous index load at
        boot) so the first filesystem-triggered pass does not re-emit `pm.item_changed` for notes
        that have not actually changed."""
        self._known_hashes = {item.id: item.note_hash for item in items}

    def start(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("pm.watcher_no_loop", note="no running event loop; live watch disabled")
            return
        observer = Observer()
        handler = _Handler(self._schedule)
        watched_any = False
        for folder in self._repo.watched_dirs():
            if folder.is_dir():
                observer.schedule(handler, str(folder), recursive=False)
                watched_any = True
        if not watched_any:
            log.info("pm.watcher_no_folders")
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def _schedule(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._debounce)

    def _debounce(self) -> None:
        if self._loop is None:
            return
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_handle = self._loop.call_later(self._debounce_s, self._fire)

    def _fire(self) -> None:
        self._debounce_handle = None
        asyncio.ensure_future(self.reindex_once())

    async def reindex_once(self) -> list[str]:
        """Rebuild the index from the vault and emit `pm.item_changed` once per id whose
        `note_hash` differs from the last known hash. Returns the changed ids."""
        items = await asyncio.to_thread(self._repo.all_items)
        changed_ids = [
            item.id for item in items if self._known_hashes.get(item.id) != item.note_hash
        ]
        await asyncio.to_thread(self._index.rebuild, items)
        self._known_hashes = {item.id: item.note_hash for item in items}
        self._reindex_count += 1
        by_id = {item.id: item for item in items}
        for item_id in changed_ids:
            item = by_id[item_id]
            await self._bus.publish(
                Event(
                    name=E.PM_ITEM_CHANGED,
                    payload={
                        "id": item.id,
                        "kind": item.kind,
                        "status": item.status,
                        "epic_id": item.epic_id,
                        "project_id": item.project_id,
                    },
                )
            )
        if self._focus is not None:
            await self._focus.recompute_and_publish()
        return changed_ids
