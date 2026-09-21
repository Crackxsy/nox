"""Vault-backed repository for Project/Epic/Story notes: parses and writes Markdown notes with YAML
frontmatter under the configured vault folders, preserving every frontmatter field and the body a
write did not touch. `pyyaml` reads the frontmatter into a plain mapping; writing a targeted field
(e.g. `status`) is done by a line-level regex replace on the original frontmatter text, so an
unrelated field and the body stay byte-identical ("only status/updated change") without round-
tripping through a YAML dumper that could reformat lists, quoting or key order.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from nox.core.logging import get_logger
from nox.pm.models import REQUIRED_FIELDS, Kind, WorkItem, WorkItemValidationError

log = get_logger(__name__)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<fm>.*?)\r?\n---\r?\n?(?P<body>.*)\Z", re.DOTALL)
_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):")
_EPIC_ID_RE = re.compile(r"^(EPIC-\d+)")
_STORY_ID_RE = re.compile(r"^(ST-\d+-\d+)")
# PRD's conflicting `NOX-<epic>.<story>` id scheme (e.g. `NOX-13.1`), distinct from the template
# convention (`NOX-`/`NOX-`) this repo accepts (ES-06, pending approval).
_PRD_DOC_ID_RE = re.compile(r"^NOX-\d+\.\d+$")

_ID_RE_BY_KIND: dict[Kind, re.Pattern[str]] = {"epic": _EPIC_ID_RE, "story": _STORY_ID_RE}


class VaultNoteError(ValueError):
    """Malformed note: no frontmatter block, or frontmatter that is not a YAML mapping."""


@dataclass(frozen=True, slots=True)
class VaultNote:
    """One parsed Markdown note: the raw frontmatter text (for surgical rewrites), the parsed
    mapping, and the body untouched."""

    path: Path
    frontmatter_text: str
    frontmatter: dict[str, Any]
    body: str
    raw_hash: str


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_note(path: Path) -> VaultNote:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise VaultNoteError(f"{path}: no YAML frontmatter block")
    fm_text = match.group("fm")
    body = match.group("body")
    frontmatter = yaml.safe_load(fm_text)
    if not isinstance(frontmatter, dict):
        raise VaultNoteError(f"{path}: frontmatter is not a mapping")
    return VaultNote(
        path=path,
        frontmatter_text=fm_text,
        frontmatter=frontmatter,
        body=body,
        raw_hash=_hash_text(text),
    )


def render_note(frontmatter_text: str, body: str) -> str:
    return f"---\n{frontmatter_text}\n---\n{body}"


def write_note_fields(note: VaultNote, updates: dict[str, str]) -> str:
    """Return new raw note text with only the given top-level scalar frontmatter fields changed
    (added at the end if absent) - every other byte, including field order and untouched fields,
    is preserved."""
    remaining = dict(updates)
    out: list[str] = []
    for line in note.frontmatter_text.split("\n"):
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match is not None and match.group(1) in remaining:
            key = match.group(1)
            out.append(f"{key}: {remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}: {value}")
    return render_note("\n".join(out), note.body)


def _scalar(value: Any) -> str | None:
    """YAML dates parse as `datetime.date`; coerce every scalar frontmatter value to `str` for the
    flat `WorkItem` model."""
    return None if value is None else str(value)


def _to_work_item(note: VaultNote, *, kind: Kind, vault_dir: Path) -> WorkItem:
    fm = note.frontmatter
    for field in REQUIRED_FIELDS[kind]:
        if field not in fm or fm[field] in (None, ""):
            raise WorkItemValidationError(field, "missing required field", note_path=str(note.path))

    id_re = _ID_RE_BY_KIND[kind]
    id_match = id_re.match(note.path.stem)
    if id_match is None:
        raise WorkItemValidationError(
            "id",
            f"filename does not start with a {kind} id ({id_re.pattern})",
            note_path=str(note.path),
        )
    item_id = id_match.group(1)

    machine_data = fm.get("machine-data")
    document_id = str(machine_data.get("document_id", "")) if isinstance(machine_data, dict) else ""
    if document_id and _PRD_DOC_ID_RE.match(document_id):
        log.warning("pm.id_scheme_prd_form", note=str(note.path), document_id=document_id)

    epic_id = str(fm["epic"]) if kind == "story" and fm.get("epic") else None
    project_id = _scalar(fm.get("project"))

    try:
        rel_path = str(note.path.relative_to(vault_dir))
    except ValueError:
        rel_path = str(note.path)

    return WorkItem(
        id=item_id,
        kind=kind,
        title=str(fm["title"]),
        status=str(fm["status"]),
        priority=_scalar(fm.get("priority")),
        estimate=_scalar(fm.get("estimate")),
        epic_id=epic_id,
        project_id=project_id,
        note_path=rel_path,
        note_hash=note.raw_hash,
        created=_scalar(fm.get("created")),
        updated=_scalar(fm.get("updated")),
    )


class PmVaultRepo:
    """Reads/writes Project/Epic/Story notes under the configured vault folders. `pm.project.list`
    reads a single "projects list note" (this vault has no per-project folder yet, unlike Epics/
    Stories) whose frontmatter carries a `projects: [{id, name, status,...}]` list."""

    def __init__(
        self, vault_dir: Path | str, *, epics_dir: str, stories_dir: str, projects_note: str
    ) -> None:
        self.vault_dir = Path(vault_dir)
        self.epics_dir = self.vault_dir / epics_dir
        self.stories_dir = self.vault_dir / stories_dir
        self.projects_note_path = self.vault_dir / projects_note

    # ---- read ----------------------------------------------------------------------------------

    def iter_epic_notes(self) -> Iterator[Path]:
        """Only `EPIC-nn...md`; an overview or map note in the same folder is not a work item."""
        if self.epics_dir.is_dir():
            yield from sorted(p for p in self.epics_dir.glob("*.md") if _EPIC_ID_RE.match(p.stem))

    def iter_story_notes(self) -> Iterator[Path]:
        if self.stories_dir.is_dir():
            yield from sorted(
                p for p in self.stories_dir.glob("*.md") if _STORY_ID_RE.match(p.stem)
            )

    def watched_dirs(self) -> list[Path]:
        return [self.epics_dir, self.stories_dir]

    def read_epic(self, path: Path) -> WorkItem:
        return _to_work_item(read_note(path), kind="epic", vault_dir=self.vault_dir)

    def read_story(self, path: Path) -> WorkItem:
        return _to_work_item(read_note(path), kind="story", vault_dir=self.vault_dir)

    def read_projects(self) -> list[WorkItem]:
        if not self.projects_note_path.is_file():
            return []
        note = read_note(self.projects_note_path)
        projects = note.frontmatter.get("projects")
        if not isinstance(projects, list):
            raise WorkItemValidationError(
                "projects", "must be a list", note_path=str(self.projects_note_path)
            )
        rel_path = str(self.projects_note_path.relative_to(self.vault_dir))
        items: list[WorkItem] = []
        for entry in projects:
            if not isinstance(entry, dict):
                raise WorkItemValidationError(
                    "projects[]", "must be a mapping", note_path=str(self.projects_note_path)
                )
            for field in REQUIRED_FIELDS["project"]:
                if field not in entry or entry[field] in (None, ""):
                    raise WorkItemValidationError(
                        field, "missing required field", note_path=str(self.projects_note_path)
                    )
            items.append(
                WorkItem(
                    id=str(entry["id"]),
                    kind="project",
                    title=str(entry["name"]),
                    status=str(entry["status"]),
                    priority=_scalar(entry.get("priority")),
                    note_path=rel_path,
                    note_hash=note.raw_hash,
                    created=_scalar(entry.get("created")),
                    updated=_scalar(entry.get("updated")),
                )
            )
        return items

    def all_items(self) -> list[WorkItem]:
        items = list(self.read_projects())
        for path in self.iter_epic_notes():
            items.append(self.read_epic(path))
        for path in self.iter_story_notes():
            items.append(self.read_story(path))
        return items

    def find_story_path(self, story_id: str) -> Path | None:
        for path in self.iter_story_notes():
            if _STORY_ID_RE.match(path.stem) and path.stem.startswith(story_id):
                return path
        return None

    # ---- write -----------------------------------------------------------------------------------

    def update_story_status(self, story_id: str, new_status: str) -> WorkItem:
        path = self.find_story_path(story_id)
        if path is None:
            raise KeyError(f"unknown story: {story_id!r}")
        note = read_note(path)
        today = datetime.now(UTC).date().isoformat()
        new_text = write_note_fields(note, {"status": new_status, "updated": today})
        path.write_text(new_text, encoding="utf-8")
        return _to_work_item(read_note(path), kind="story", vault_dir=self.vault_dir)

    def create_story(
        self,
        *,
        story_id: str,
        epic_id: str,
        title: str,
        priority: str = "P2",
        estimate: str = "M",
        requirements: list[str] | None = None,
    ) -> WorkItem:
        self.stories_dir.mkdir(parents=True, exist_ok=True)
        path = self.stories_dir / f"{story_id} {title}.md"
        if path.exists():
            raise FileExistsError(str(path))
        today = datetime.now(UTC).date().isoformat()
        frontmatter = {
            "title": f"{story_id} {title}",
            "type": "story",
            "status": "todo",
            "epic": epic_id,
            "priority": priority,
            "estimate": estimate,
            "created": today,
            "updated": today,
            "tags": ["nox", "story"],
            "machine-data": {
                "document_id": f"NOX-{story_id}",
                "requirements": list(requirements or []),
                "tests": [],
            },
        }
        fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
        # Empty headings, never a filled-in placeholder sentence: a section the user has not
        # written yet should look unwritten, not like a half-finished template.
        body = (
            f"\n# {story_id} {title}\n\n"
            "## Ziel\n\n## Akzeptanzkriterien\n\n"
            "## Sicherheit und Privatsphäre\n\n## Tests\n\n## Notizen\n"
        )
        path.write_text(render_note(fm_text, body), encoding="utf-8")
        return _to_work_item(read_note(path), kind="story", vault_dir=self.vault_dir)
