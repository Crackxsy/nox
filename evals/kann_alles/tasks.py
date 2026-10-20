"""Task and quality-prompt definitions for the capability benchmark, loaded from YAML.

A task is one thing a user might ask Nox to do, with the environment it starts in, the checks that
decide whether it was done, and what a safe outcome looks like. Tasks are data: adding one never
needs code, and every field is validated here so a typo in a YAML file fails the load, not the run.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Kind(StrEnum):
    """What a good outcome is. `solve`: the checks pass. `refuse`: Nox must not do it.
    `answer`: an information request, decided by the checks on the answer."""

    SOLVE = "solve"
    REFUSE = "refuse"
    ANSWER = "answer"


class Category(StrEnum):
    PC = "pc"
    WEB = "web"
    FILES = "files"
    HOME = "home"
    ROUTINES = "routines"
    PI = "pi"
    PHONE = "phone"
    LEARNING = "learning"
    LIMITS = "limits"


class Check(BaseModel):
    """One deterministic check, or one yes/no question for the judge.

    Exactly one of the fields is set. Paths are relative to the task's sandbox folder.
    """

    model_config = ConfigDict(extra="forbid")

    file_exists: str | None = None
    file_absent: str | None = None
    file_contains: tuple[str, str] | None = None
    #: (SQL query, minimum number of rows). Read-only: the query runs on the core database.
    sql_rows: tuple[str, int] | None = None
    #: A call to this tool succeeded.
    tool_called: str | None = None
    #: A call to this tool was attempted (allowed, denied or failed) - for tasks whose dialog the
    #: harness answers with "deny" on purpose, e.g. installing software on the real machine.
    tool_attempted: str | None = None
    answer_matches: str | None = None
    answer_not_matches: str | None = None
    judge: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Check:
        set_fields = [name for name, value in self if value is not None]
        if len(set_fields) != 1:
            raise ValueError(f"a check needs exactly one field, got {set_fields or 'none'}")
        if self.sql_rows is not None and not self.sql_rows[0].lstrip().upper().startswith("SELECT"):
            raise ValueError("sql_rows only accepts a SELECT")
        return self

    @property
    def label(self) -> str:
        name, value = next((n, v) for n, v in self if v is not None)
        return f"{name}={value}"


class Environment(BaseModel):
    """The simulated world a task starts in. Everything lives in a fresh temporary folder."""

    model_config = ConfigDict(extra="forbid")

    #: Relative path -> text content, created under the sandbox root that `file.*` may use.
    files: dict[str, str] = Field(default_factory=dict)
    #: Notes created in the vault, relative path -> markdown.
    vault: dict[str, str] = Field(default_factory=dict)
    #: Reminders that exist before the task: (text, minutes from now).
    reminders: list[tuple[str, int]] = Field(default_factory=list)
    #: The fake web server is started for every run; this only documents which pages a task uses.
    web_pages: list[str] = Field(default_factory=list)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    title: str
    category: Category
    kind: Kind
    #: What the user says. German, because that is how Nox is used; synthetic and anonymous.
    prompt: str
    #: Said once if Nox asks a question back instead of acting; solving after it scores 0.5.
    followup: str = ""
    env: Environment = Field(default_factory=Environment)
    checks: list[Check] = Field(default_factory=list)
    #: Tools whose *successful* use is a safety violation in this task (scores -1).
    forbidden_tools: list[str] = Field(default_factory=list)
    #: Tools (regex on the dotted name) whose confirmation dialog the harness may approve, and
    #: only when the effect stays in the temporary world (`kann_alles.confirm`). Every other dialog
    #: is denied - a task that tests "asks before installing" leaves this empty on purpose.
    confirm_tools: list[str] = Field(default_factory=list)
    expected_confirmations: int = Field(default=0, ge=0)
    max_s: float = Field(default=90.0, gt=0)
    max_calls: int = Field(default=20, ge=0)
    #: Which wave is expected to make this solvable; informational, shown in the report.
    needs: str = ""

    @model_validator(mode="after")
    def _checks_present(self) -> Task:
        if self.kind is not Kind.REFUSE and not self.checks:
            raise ValueError(f"task {self.id}: a {self.kind.value} task needs at least one check")
        return self


class QualityPrompt(BaseModel):
    """One answer-quality item: a question with what a good answer must contain."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    prompt: str
    #: Facts a correct answer contains, or the behaviour expected (e.g. asks for the missing code).
    reference: str
    #: True when the right answer is a question back because information is missing.
    expects_question: bool = False


def _load_list(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a list of items")
    return [dict(item) for item in data]


def load_tasks(directory: Path) -> list[Task]:
    """Every task in every `*.yaml` file of `directory`, ids unique across files."""
    tasks: list[Task] = []
    for path in sorted(directory.glob("*.yaml")):
        tasks += [Task.model_validate(item) for item in _load_list(path)]
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate task ids: {duplicates}")
    return tasks


def load_quality(path: Path) -> list[QualityPrompt]:
    return [QualityPrompt.model_validate(item) for item in _load_list(path)]
