"""nox.core.tasks: persistence, priority, checkpoints, failure recording, game pause."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nox.core.bus import AsyncEventBus
from nox.core.events import E, Event
from nox.core.tasks import TaskQueue, mode_change_means_game
from nox.data.db import Database
from nox.data.repos import TaskRepository, TaskRow, TaskStatus


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[TaskRepository]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    yield TaskRepository(db)
    db.close()


def test_mode_change_means_game() -> None:
    assert mode_change_means_game({"current": "rocket_league"})
    assert mode_change_means_game({"current": "stream", "layers": ["rocket_league"]})
    assert mode_change_means_game({"current": "stream", "game_running": True})
    assert not mode_change_means_game({"current": "coding"})


async def test_run_next_priority_checkpoint_and_failure(repo: TaskRepository) -> None:
    bus = AsyncEventBus()
    events: list[str] = []
    bus.subscribe("task.*", lambda ev: events.append(ev.name))
    queue = TaskQueue(bus, repo)
    ran: list[str] = []

    async def work(task: TaskRow, checkpoint: Any) -> None:
        ran.append(task.payload["n"])
        checkpoint({"step": 1})

    async def fail(task: TaskRow, checkpoint: Any) -> None:
        raise RuntimeError("nope")

    queue.register("work", work)
    queue.register("fail", fail)
    with pytest.raises(KeyError):
        await queue.submit("unknown", {})
    low = await queue.submit("work", {"n": "low"}, priority=1)
    high = await queue.submit("work", {"n": "high"}, priority=9)
    failing = await queue.submit("fail", {}, priority=5)
    assert (await queue.run_next()).id == high.id  # type: ignore[union-attr]
    result = await queue.run_next()
    assert result is not None and result.id == failing.id and result.status is TaskStatus.FAILED
    assert "nope" in result.error
    assert (await queue.run_next()).id == low.id  # type: ignore[union-attr]
    assert await queue.run_next() is None
    assert ran == ["high", "low"]
    assert repo.get(high.id).checkpoint == {"step": 1}  # type: ignore[union-attr]
    assert events == [
        "task.started",
        "task.finished",
        "task.started",
        "task.failed",
        "task.started",
        "task.finished",
    ]


async def test_loop_pauses_while_game_runs(repo: TaskRepository) -> None:
    bus = AsyncEventBus()
    queue = TaskQueue(bus, repo, poll_interval_s=0.02)
    done: list[str] = []

    async def work(task: TaskRow, checkpoint: Any) -> None:
        done.append(task.id)

    queue.register("work", work)
    repo.add("work", task_id="crashed")
    repo.set_status("crashed", TaskStatus.RUNNING)  # left over from a crash
    queue.start()
    await asyncio.sleep(0.1)
    assert done == ["crashed"]

    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={
                "previous": "companion",
                "current": "rocket_league",
            },
        )
    )
    assert queue.paused
    t = await queue.submit("work", {})
    await asyncio.sleep(0.1)
    assert t.id not in done
    await bus.publish(
        Event(
            name=E.SYSTEM_MODE_CHANGED,
            payload={
                "previous": "rocket_league",
                "current": "companion",
            },
        )
    )
    assert not queue.paused
    await asyncio.sleep(0.1)
    assert t.id in done
    await queue.stop()
