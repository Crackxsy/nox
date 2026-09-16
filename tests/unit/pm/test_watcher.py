"""`nox.pm.watcher.PmWatcher`: debounces a burst of changes into one reindex, is idempotent for an
unchanged vault, and reconciles by `note_hash` rather than re-emitting `pm.item_changed`.
"""

from __future__ import annotations

import asyncio

from nox.core.events import E
from nox.pm.index import PmIndex
from nox.pm.vault_repo import PmVaultRepo
from nox.pm.watcher import PmWatcher
from tests.unit.fakes import FakeBus


async def test_burst_of_schedule_calls_collapses_into_one_reindex(
    repo: PmVaultRepo, index: PmIndex
) -> None:
    bus = FakeBus()
    watcher = PmWatcher(repo, index, bus, debounce_s=0.05)
    watcher._loop = asyncio.get_running_loop()  # start() would spin a real Observer thread

    for _ in range(5):
        watcher._schedule()
        await asyncio.sleep(0.01)  # well inside the 0.05s debounce window

    await asyncio.sleep(0.15)  # past the debounce window + one reindex pass
    assert watcher.reindex_count == 1


async def test_reindex_once_is_idempotent_for_an_unchanged_vault(
    repo: PmVaultRepo, index: PmIndex
) -> None:
    bus = FakeBus()
    watcher = PmWatcher(repo, index, bus, debounce_s=0.01)
    changed_first = await watcher.reindex_once()
    assert set(changed_first) == {"PRJ-NOX", "EPIC-13", "ST-13-01", "ST-13-02"}
    bus.published.clear()

    changed_second = await watcher.reindex_once()
    assert changed_second == []
    assert not any(e.name == E.PM_ITEM_CHANGED for e in bus.published)


async def test_reindex_once_emits_only_for_changed_ids(repo: PmVaultRepo, index: PmIndex) -> None:
    bus = FakeBus()
    watcher = PmWatcher(repo, index, bus, debounce_s=0.01)
    await watcher.reindex_once()
    bus.published.clear()

    repo.update_story_status("ST-13-01", "in_progress")
    changed = await watcher.reindex_once()

    assert changed == ["ST-13-01"]
    item_changed_events = [e for e in bus.published if e.name == E.PM_ITEM_CHANGED]
    assert len(item_changed_events) == 1
    assert item_changed_events[0].payload["id"] == "ST-13-01"
    assert item_changed_events[0].payload["status"] == "in_progress"


async def test_seed_known_hashes_prevents_reemission_on_first_pass(
    repo: PmVaultRepo, index: PmIndex
) -> None:
    bus = FakeBus()
    items = repo.all_items()
    index.rebuild(items)
    watcher = PmWatcher(repo, index, bus, debounce_s=0.01)
    watcher.seed_known_hashes(items)

    changed = await watcher.reindex_once()
    assert changed == []
