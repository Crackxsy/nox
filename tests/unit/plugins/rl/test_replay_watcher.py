"""Replay watcher (ST-12-02): new-file detection, stability wait, backlog vs. new-file separation,
and never crashing when the folder is temporarily unreachable."""

from __future__ import annotations

import asyncio

import pytest
from nox_plugin_rl.replay_watcher import ReplayWatcher

pytestmark = pytest.mark.timeout(30)


async def test_seed_known_marks_existing_files_without_calling_back(tmp_path) -> None:
    (tmp_path / "a.replay").write_bytes(b"x")
    seen: list[str] = []

    async def on_new(path):
        seen.append(path.name)

    watcher = ReplayWatcher(tmp_path, on_new_file=on_new)
    files = watcher.seed_known()
    assert [f.name for f in files] == ["a.replay"]
    assert seen == []


async def test_new_file_is_queued_once_stable(tmp_path) -> None:
    seen: list[str] = []

    async def on_new(path):
        seen.append(path.name)

    watcher = ReplayWatcher(
        tmp_path,
        on_new_file=on_new,
        poll_interval_s=0.05,
        stable_check_interval_s=0.02,
        stable_checks=2,
    )
    watcher.start()
    try:
        (tmp_path / "b.replay").write_bytes(b"data")
        await asyncio.wait_for(_wait_for(lambda: seen == ["b.replay"]), timeout=5)
    finally:
        await watcher.stop()


async def test_wait_stable_returns_false_for_a_missing_file(tmp_path) -> None:
    watcher = ReplayWatcher(
        tmp_path, on_new_file=lambda p: asyncio.sleep(0), stable_check_interval_s=0.01
    )
    missing = tmp_path / "gone.replay"
    assert await watcher.wait_stable(missing) is False


async def test_backlog_seed_calls_the_backlog_callback_for_every_pre_existing_file(
    tmp_path,
) -> None:
    (tmp_path / "a.replay").write_bytes(b"x")
    (tmp_path / "b.replay").write_bytes(b"y")
    backlog: list[str] = []

    watcher = ReplayWatcher(
        tmp_path,
        on_new_file=lambda p: asyncio.sleep(0),
        on_backlog_file=lambda p: backlog.append(p.name) or asyncio.sleep(0),
    )
    await watcher.seed_backlog()
    assert sorted(backlog) == ["a.replay", "b.replay"]


async def _wait_for(predicate, *, interval: float = 0.05) -> None:
    while not predicate():  # noqa: ASYNC110 - simple test polling helper, no event to wait on
        await asyncio.sleep(interval)
