"""`files` configuration: which folders exist for Nox at all.

This section is the whole security boundary of the file tools, and it ships empty on purpose. A
fresh installation can list, read, write and move nothing outside the vault, and the tools say so
by name instead of failing in some confusing way. Adding a folder here is a deliberate act, the same
way registering a program for a preset is.

The list is not a suggestion the model can widen. It is read at call time, the path is resolved
first - so a junction or `..` cannot lead out of a root - and a path outside every root never
reaches an I/O call.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from nox.core.config.types import StrictSection

__all__ = ["FilesConfig"]

#: Reading a file puts its content in a prompt. A quarter of a megabyte is already more than any
#: answer needs, and without a cap one wrong path turns a turn into a very expensive one.
DEFAULT_MAX_READ_BYTES = 256 * 1024


class FilesConfig(StrictSection):
    """The `files` section of `config/defaults.yaml`."""

    enabled: bool = True
    #: Folders the file tools may touch. Empty means none at all.
    roots: list[str] = Field(default_factory=list)
    max_read_bytes: int = Field(default=DEFAULT_MAX_READ_BYTES, ge=1024, le=8 * 1024 * 1024)
    #: Entries one listing may return. A folder with 40000 files must not become one answer.
    max_list_entries: int = Field(default=200, ge=1, le=5000)
    #: Bytes one write may carry. Generous for text, small enough that a runaway loop is bounded.
    max_write_bytes: int = Field(default=DEFAULT_MAX_READ_BYTES, ge=1, le=8 * 1024 * 1024)
    #: Delete by moving to the Windows Recycle Bin. With this off there is no delete tool at all -
    #: an unlink that cannot be undone is not something to hand a language model.
    delete_to_recycle_bin: bool = True

    @field_validator("roots")
    @classmethod
    def _no_blank_roots(cls, value: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in value if entry.strip()]
        if len(cleaned) != len(value):
            raise ValueError("a blank entry in files.roots would be read as 'nowhere'; remove it")
        return cleaned
