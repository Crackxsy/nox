"""`nox.pm.index.PmIndex`: rebuild from the vault, upsert, filtered listing."""

from __future__ import annotations

from nox.pm.index import PmIndex
from nox.pm.vault_repo import PmVaultRepo


def test_rebuild_indexes_every_item(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    assert {i.id for i in index.list_items()} == {"PRJ-NOX", "EPIC-13", "ST-13-01", "ST-13-02"}


def test_rebuild_replaces_stale_rows(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    index.rebuild([])  # vault now empty -> mirror empties too (vault wins)
    assert index.list_items() == []


def test_list_items_filters_by_kind_and_status(repo: PmVaultRepo, index: PmIndex) -> None:
    index.rebuild(repo.all_items())
    stories = index.list_items(kind="story")
    assert {i.id for i in stories} == {"ST-13-01", "ST-13-02"}
    todo = index.list_items(kind="story", statuses=("todo",))
    assert [i.id for i in todo] == ["ST-13-01"]


def test_get_returns_none_for_unknown_id(index: PmIndex) -> None:
    assert index.get("ST-99-99") is None


def test_upsert_updates_a_single_row(repo: PmVaultRepo, index: PmIndex) -> None:
    items = repo.all_items()
    index.rebuild(items)
    updated = repo.update_story_status("ST-13-01", "in_progress")
    index.upsert(updated)
    assert index.get("ST-13-01").status == "in_progress"  # type: ignore[union-attr]
    # other rows untouched
    assert index.get("ST-13-02").status == "in_progress"  # type: ignore[union-attr]
