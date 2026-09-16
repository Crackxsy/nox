"""The replay backfill must not block the event loop: parsing thousands of replays used to run
synchronously on the loop and stalled the core for over a minute at boot (2026-09-16)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.core.bus import AsyncEventBus
from nox.data.db import Database
from nox.data.rl_repos import RlReplayRepository
from nox.rl import install as rl_install
from nox.rl import replay_parser


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    yield database
    database.close()


class _Parsed:
    parse_status = "failed"

    def summary(self) -> dict[str, Any]:
        return {}


async def test_backfill_parses_off_loop_and_yields_between_files(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "replays"
    folder.mkdir()
    for i in range(6):
        (folder / f"match-{i}.replay").write_bytes(b"\x00")

    def slow_parse(_path: str) -> _Parsed:
        time.sleep(0.05)  # blocking CPU work, as the real parser
        return _Parsed()

    monkeypatch.setattr(replay_parser, "parse_replay_file", slow_parse)
    core = SimpleNamespace(db=db, bus=AsyncEventBus())
    cfg = SimpleNamespace(replay=SimpleNamespace(folder=str(folder), backfill_batch_size=2))
    rl_install._install_backfill_task(core, cfg, RlReplayRepository(db))
    queue = core.rl_backfill_queue
    handler = queue._handlers[rl_install._BACKFILL_KIND]

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    checkpoints: list[dict[str, Any]] = []
    task = asyncio.create_task(ticker())
    try:
        await handler(SimpleNamespace(checkpoint=None), checkpoints.append)
    finally:
        task.cancel()
        await queue.stop()
    assert ticks >= 10  # ~300 ms of parsing, the loop kept turning
    assert checkpoints[-1] == {"index": 6}
