"""Typed work-item model for PM index: Project/Epic/Story frontmatter mapped to one consistent
shape.
Deliberately small - dependency edges, the status-workflow engine, ADRs/bugs/portfolio are later PM
stories, not built here (see this story's report Open Points).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Kind = Literal["project", "epic", "story"]

# Required frontmatter keys per kind, mirroring `15 - Templates/{Epic,Story} Template.md`. `status`
# is intentionally free-text (not a closed enum) - this vault's real epics already use values like
# "proposed"/"active"/"pending approval" alongside the story workflow's "todo" (P8 dogfooding,
#: existing notes must round-trip without data loss).
REQUIRED_FIELDS: dict[Kind, tuple[str, ...]] = {
    "epic": ("title", "type", "status", "priority", "created", "updated", "tags"),
    "story": (
        "title",
        "type",
        "status",
        "epic",
        "priority",
        "estimate",
        "created",
        "updated",
        "tags",
    ),
    "project": ("id", "name", "status"),
}


class WorkItemValidationError(ValueError):
    """A vault note failed schema validation. `field` names the offending frontmatter key, ("a
    specific field-level error, not a generic parse failure")."""

    def __init__(self, field: str, message: str, *, note_path: str = "") -> None:
        self.field = field
        self.note_path = note_path
        text = f"{note_path}: field {field!r}: {message}" if note_path else f"{field!r}: {message}"
        super().__init__(text)


class WorkItem(BaseModel):
    """One row of the PM index (`pm_items`), mirrored from a vault note or a `projects` list
    entry. `id` is the filename-prefix id already used throughout this vault (``,
    ``) - the template convention accepts over the PRD's conflicting
    `NOX-<epic>.<story>` form (ES-06, pending approval)."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: Kind
    title: str
    status: str
    priority: str | None = None
    estimate: str | None = None
    epic_id: str | None = None
    project_id: str | None = None
    note_path: str
    note_hash: str
    created: str | None = None
    updated: str | None = None
