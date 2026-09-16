"""Fake vault dir with real template-shaped notes (`15 - Templates/{Epic,Story} Template.md`
field sets) - used by every `tests/unit/pm` module."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.data.db import Database
from nox.pm.index import PmIndex
from nox.pm.vault_repo import PmVaultRepo

EPIC_NOTE = """---
title: "EPIC-13 Test Epic"
type: epic
status: proposed
priority: P1
release: v0.4
owner: nox-implementation
created: 2026-09-09
updated: 2026-09-09
tags: [nox, epic, project-management]
machine-data:
  document_id: NOX-EPIC-13
  requirements: [FR-11.1]
  stories: [ST-13-01, ST-13-02]
---

# EPIC-13 Test Epic

## Goal
Fixture epic for `tests/unit/pm`.
"""

STORY_TODO_NOTE = """---
title: "ST-13-01 Todo Story"
type: story
status: todo
epic: EPIC-13
priority: P1
estimate: L
created: 2026-09-09
updated: 2026-09-09
tags: [nox, story, pm]
machine-data:
  document_id: NOX-ST-13-01
  requirements: [FR-11.1]
  tests: [tests/unit/pm/test_vault_repo.py]
---

# ST-13-01 Todo Story

**As** Nox, **I want** a fixture, **so that** tests pass.

## Acceptance criteria
- [ ] Given a fixture, when read, then it round-trips.

## Security / privacy impact
None.
## Tests
Fixture only.
## Notes
Fixture only.
"""

STORY_IN_PROGRESS_NOTE = """---
title: "ST-13-02 In Progress Story"
type: story
status: in_progress
epic: EPIC-13
priority: P2
estimate: M
created: 2026-09-10
updated: 2026-09-10
tags: [nox, story, pm]
machine-data:
  document_id: NOX-ST-13-02
  requirements: []
  tests: []
---

# ST-13-02 In Progress Story
Fixture only.
"""

STORY_MISSING_ESTIMATE_NOTE = """---
title: "ST-13-09 Broken Story"
type: story
status: todo
epic: EPIC-13
priority: P3
created: 2026-09-11
updated: 2026-09-11
tags: [nox, story, pm]
machine-data:
  document_id: NOX-ST-13-09
  requirements: []
  tests: []
---

# ST-13-09 Broken Story
Missing `estimate` on purpose.
"""

PROJECTS_NOTE = """---
title: "Projects"
type: project-list
projects:
  - id: PRJ-NOX
    name: Nox
    status: active
    priority: P1
    created: 2026-09-09
    updated: 2026-09-09
---

# Projects
Fixture projects list note.
"""


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "08 - Epics").mkdir(parents=True)
    (root / "09 - Stories").mkdir(parents=True)
    (root / "08 - Epics" / "EPIC-13 Test Epic.md").write_text(EPIC_NOTE, encoding="utf-8")
    (root / "09 - Stories" / "ST-13-01 Todo Story.md").write_text(STORY_TODO_NOTE, encoding="utf-8")
    (root / "09 - Stories" / "ST-13-02 In Progress Story.md").write_text(
        STORY_IN_PROGRESS_NOTE, encoding="utf-8"
    )
    (root / "Projects.md").write_text(PROJECTS_NOTE, encoding="utf-8")
    return root


@pytest.fixture
def repo(vault_dir: Path) -> PmVaultRepo:
    return PmVaultRepo(
        vault_dir, epics_dir="08 - Epics", stories_dir="09 - Stories", projects_note="Projects.md"
    )


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.migrate()
    return database


@pytest.fixture
def index(db: Database) -> PmIndex:
    return PmIndex(db)
