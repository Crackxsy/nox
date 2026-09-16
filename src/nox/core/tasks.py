"""TaskQueue: minimal prioritised, checkpointed background task queue (Data Model `tasks`, FR-10.4).

Tasks are persisted through `TaskRepository` before they run, so a crash never loses them; a handler
receives a `checkpoint(dict)` callback and RUNNING tasks are reset to PENDING at startup with their
last checkpoint intact. The queue pauses while a game is running (`system.mode_changed` with
`game_running=True` or a rocket_league mode/layer) and resumes on the next mode change without it.
Higher `priority` runs first. Events `task.started|finished|failed` carry `{id, kind}` dicts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.data.repos import TaskRepository, TaskRow, TaskStatus

CheckpointFn = Callable[[dict[str, Any]], None]
TaskHandler = Callable[[TaskRow, CheckpointFn], Awaitable[None]]
GAME_MODES: frozenset[str] = frozenset({"rocket_league"})


def mode_change_means_game(payload: dict[str, Any]) -> bool:
    if bool(payload.get("game_running", False)):
        return True
    if str(payload.get("current", "")) in GAME_MODES:
        return True
    layers = payload.get("layers") or []
    return any(str(layer) in GAME_MODES for layer in layers)


class TaskQueue:
    def __init__(
        self, bus: EventBus, repo: TaskRepository, *, poll_interval_s: float = 0.5
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._poll = poll_interval_s
        self._handlers: dict[str, TaskHandler] = {}
        self._log = get_logger(__name__)
        self._paused = False
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._current: str | None = None

    # ---- configuration -----------------------------------------------------------------------

    def register(self, kind: str, handler: TaskHandler) -> None:
        self._handlers[kind] = handler

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def current_task_id(self) -> str | None:
        return self._current

    def pause(self, reason: str = "") -> None:
        if not self._paused:
            self._paused = True
            self._log.info("tasks.paused", reason=reason)

    def resume(self, reason: str = "") -> None:
        if self._paused:
            self._paused = False
            self._log.info("tasks.resumed", reason=reason)
            self._wake.set()

    # ---- submission --------------------------------------------------------------------------

    async def submit(
        self, kind: str, payload: dict[str, Any] | None = None, *, priority: int = 0
    ) -> TaskRow:
        if kind not in self._handlers:
            raise KeyError(f"no handler registered for task kind {kind!r}")
        row = self._repo.add(kind, payload, priority=priority)
        self._wake.set()
        return row

    def recover(self) -> int:
        """Reset tasks left RUNNING by a crash back to PENDING (checkpoints are kept)."""
        return self._repo.reset_running()

    # ---- execution ---------------------------------------------------------------------------

    async def run_next(self) -> TaskRow | None:
        """Run the highest-priority pending task, if any. Returns the finished/failed row."""
        row = self._repo.next_pending()
        if row is None:
            return None
        handler = self._handlers.get(row.kind)
        if handler is None:
            self._repo.set_status(
                row.id, TaskStatus.FAILED, error=f"no handler for kind {row.kind!r}"
            )
            await self._emit("task.failed", row, error="no handler")
            return self._repo.get(row.id)

        self._repo.set_status(row.id, TaskStatus.RUNNING)
        self._current = row.id
        await self._emit("task.started", row)

        def checkpoint(data: dict[str, Any]) -> None:
            self._repo.save_checkpoint(row.id, data)

        try:
            await handler(row, checkpoint)
        except asyncio.CancelledError:
            self._repo.set_status(row.id, TaskStatus.PENDING)
            self._current = None
            raise
        except Exception as exc:  # noqa: BLE001 - a failing task is recorded, never crashes the queue
            message = f"{type(exc).__name__}: {exc}"
            self._repo.set_status(row.id, TaskStatus.FAILED, error=message)
            self._log.warning("tasks.failed", task_id=row.id, kind=row.kind, error=message)
            await self._emit("task.failed", row, error=message)
        else:
            self._repo.set_status(row.id, TaskStatus.DONE)
            await self._emit("task.finished", row)
        finally:
            self._current = None
        return self._repo.get(row.id)

    async def _emit(self, name: str, row: TaskRow, **extra: Any) -> None:
        await self._bus.publish(
            Event(name=name, payload={"id": row.id, "kind": row.kind, **extra}, source="tasks")
        )

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self.recover()
        self._unsubscribe = self._bus.subscribe(E.SYSTEM_MODE_CHANGED, self._on_mode_changed)
        self._loop_task = asyncio.create_task(self._loop(), name="nox-tasks")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def _on_mode_changed(self, event: Event) -> None:
        if mode_change_means_game(event.payload):
            self.pause(reason="game_running")
        else:
            self.resume(reason="mode_changed")

    async def _loop(self) -> None:
        while True:
            if self._paused:
                self._wake.clear()
                await self._wake.wait()
                continue
            ran = await self.run_next()
            if ran is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), self._poll)
                except TimeoutError:
                    pass
