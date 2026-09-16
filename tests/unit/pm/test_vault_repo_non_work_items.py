"""Overview/map notes that live next to the epics and stories are not work items: the repo skips
them instead of failing the whole `pm` extension on a missing `priority` (2026-09-15, the real
vault's `08 - Epics/Epics Overview.md`)."""

from __future__ import annotations

from nox.pm.vault_repo import PmVaultRepo

OVERVIEW = """---
title: Epics Overview
type: map
status: active
tags: [nox, epic, map]
---

# Epics Overview

| ID | Name |
|---|---|
"""


def test_overview_notes_are_skipped(repo: PmVaultRepo) -> None:
    (repo.epics_dir / "Epics Overview.md").write_text(OVERVIEW, encoding="utf-8")
    (repo.stories_dir / "Stories Board.md").write_text(OVERVIEW, encoding="utf-8")
    epics = [p.name for p in repo.iter_epic_notes()]
    stories = [p.name for p in repo.iter_story_notes()]
    assert epics == ["EPIC-13 Test Epic.md"]
    assert all(name.startswith("ST-") for name in stories) and stories
    # A full load must succeed with the overview present.
    items = repo.read_all() if hasattr(repo, "read_all") else None
    if items is not None:
        assert all(item.id.startswith(("EPIC-", "ST-")) for item in items)
