"""`nox.pm.focus`: ranking determinism, reason strings, change-detection event."""

from __future__ import annotations

from nox.core.events import E
from nox.pm.focus import FocusService, compute_focus
from nox.pm.index import PmIndex
from nox.pm.vault_repo import PmVaultRepo
from tests.unit.fakes import FakeBus


def test_compute_focus_ranks_in_progress_before_todo(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    entries = compute_focus(index)
    assert [e.id for e in entries] == ["ST-13-02", "ST-13-01"]
    assert entries[0].reason  # every entry carries a human-readable reason
    assert "in progress" in entries[0].reason


def test_compute_focus_is_deterministic(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    first = [e.id for e in compute_focus(index)]
    second = [e.id for e in compute_focus(index)]
    assert first == second


def test_compute_focus_respects_limit(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    entries = compute_focus(index, limit=1)
    assert len(entries) == 1


async def test_focus_service_emits_only_on_change(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    bus = FakeBus()
    service = FocusService(index, bus, limit=5)

    await service.recompute_and_publish()
    assert bus.names().count(E.PM_FOCUS_CHANGED) == 1

    await service.recompute_and_publish()  # nothing changed
    assert bus.names().count(E.PM_FOCUS_CHANGED) == 1

    repo.update_story_status("ST-13-01", "done")
    index.rebuild(repo.all_items())
    await service.recompute_and_publish()
    assert bus.names().count(E.PM_FOCUS_CHANGED) == 2
