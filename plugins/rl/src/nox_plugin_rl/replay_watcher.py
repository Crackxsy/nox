"""Replay watcher: polls the replay folder read-only, waits for a file to stabilize (size/mtime
unchanged across `stable_checks` reads) before handing it to the parser, retries with backoff if
the folder is temporarily unreachable, and separates the pre-existing backlog (low priority) from
newly-arrived files (normal priority). Deliberately simple polling rather than an OS file-system-
events API: fewer moving parts to keep correct under OneDrive sync churn, and it is what the
acceptance criteria actually require ("next scan... fires")."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from nox.core.logging import get_logger

log = get_logger(__name__)

OnFile = Callable[[Path], Awaitable[None]]


@dataclass(slots=True)
class _FileState:
    size: int
    mtime: float
    stable_reads: int = 0


class ReplayWatcher:
    def __init__(
        self,
        folder: Path,
        *,
        on_new_file: OnFile,
        on_backlog_file: OnFile | None = None,
        poll_interval_s: float = 5.0,
        stable_check_interval_s: float = 2.0,
        stable_checks: int = 2,
        min_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._folder = folder
        self._on_new_file = on_new_file
        self._on_backlog_file = on_backlog_file or on_new_file
        self._poll_interval_s = poll_interval_s
        self._stable_check_interval_s = stable_check_interval_s
        self._stable_checks = stable_checks
        self._min_backoff_s = min_backoff_s
        self._max_backoff_s = max_backoff_s
        self._known: set[str] = set()
        self._pending: dict[str, _FileState] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def list_files(self) -> list[Path]:
        try:
            return sorted(self._folder.glob("*.replay"))
        except OSError:
            return []

    def seed_known(self) -> list[Path]:
        """Mark every file already on disk as known without calling any callback - the plugin uses
        this so its live loop only reacts to genuinely new arrivals; the one-time backlog scan of
        the full folder is a separate, core-side TaskQueue job (`nox.rl.install`)."""
        files = self.list_files()
        for f in files:
            self._known.add(f.name)
        return files

    async def seed_backlog(self) -> list[Path]:
        """First-time scan: everything already on disk is backlog, queued via
        `on_backlog_file` so the caller can give it low task-queue priority."""
        files = self.list_files()
        for f in files:
            self._known.add(f.name)
        for f in files:
            await self._on_backlog_file(f)
        return files

    async def wait_stable(self, path: Path) -> bool:
        """True once `path`'s size/mtime are unchanged across `stable_checks` consecutive reads
        (AC: never parse a file still being written)."""
        last: tuple[int, float] | None = None
        stable = 0
        for _ in range(self._stable_checks + 3):
            try:
                st = await asyncio.to_thread(path.stat)
            except OSError:
                return False
            current = (st.st_size, st.st_mtime)
            if last is not None and current == last:
                stable += 1
                if stable >= self._stable_checks:
                    return True
            else:
                stable = 0
            last = current
            await asyncio.sleep(self._stable_check_interval_s)
        return False

    async def _scan_once(self) -> None:
        files = self.list_files()
        for f in files:
            if f.name in self._known:
                continue
            self._known.add(f.name)
            if await self.wait_stable(f):
                await self._on_new_file(f)
            else:
                log.warning("rl.replay_watcher.unstable", file=f.name)

    async def _loop(self) -> None:
        backoff = self._min_backoff_s
        while not self._stopped.is_set():
            try:
                if not self._folder.is_dir():
                    raise OSError(f"replay folder not reachable: {self._folder}")
                await self._scan_once()
                backoff = self._min_backoff_s
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                log.warning(
                    "rl.replay_watcher.folder_unreachable", error=str(exc), backoff_s=backoff
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self._max_backoff_s)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="rl-replay-watcher")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
