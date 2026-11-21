"""The arguments of the file and shell tools - strict: nothing the core sends beyond these fields
is accepted - and the refusal rules and summaries the core and the device both derive from them."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nox.node.device.safe_fs.formats import extension_class, structured, write_refusal
from nox.node.device.safe_fs.text_checks import content_refusal

MAX_WRITE_BYTES = 1024 * 1024


#: A write's content is shown in full up to twice this; beyond, its head and its tail are
#: shown with a marker saying how much is not shown (never a cut text that looks complete).
PREVIEW_CHARS = 200


class PathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)
    text: str = Field(max_length=MAX_WRITE_BYTES)
    append: bool = False
    overwrite: bool = False


class MoveArgs(BaseModel):
    """The core's `file.move` arguments. A node never replaces a file and never creates a folder:
    `overwrite` and `create_folders` are accepted only as false (`move_refusal`)."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)
    to: str = Field(min_length=1, max_length=400)
    overwrite: bool = False
    create_folders: bool = False


def move_refusal(path: str, to: str, *, overwrite: bool, create_folders: bool) -> str | None:
    """What refuses a move before anyone is asked - on the core and on the node: a replace, a
    new folder, or a name a write would be refused for at either end."""
    if overwrite:
        return "overwrite_refused"
    if create_folders:
        return "folders_not_created"
    source = _last_name(path)
    refusal = write_refusal([source]) if source else "invalid_path"
    if refusal is not None:
        return refusal
    target = _last_name(to)
    if not target:
        return "invalid_path"
    if "." in target.lstrip("."):  # a file name; a name without one may be a folder to move into
        if extension_class(source) != extension_class(target):
            return "extension_class_changed"
        return write_refusal([target])
    return "hidden_refused" if target.startswith(".") else None


def delete_refusal(path: str) -> str | None:
    """What refuses a delete before anyone is asked: a name a write would be refused for."""
    name = _last_name(path)
    return write_refusal([name]) if name else "invalid_path"


def _last_name(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    return parts[-1] if parts else ""


class ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str | None = Field(default=None, max_length=4000)
    argv: list[str] | None = Field(default=None, max_length=64)
    egress: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("argv")
    @classmethod
    def _short(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(len(token) > 1000 for token in value):
            raise ValueError("an argument is too long")
        return value

    @model_validator(mode="after")
    def _one_of(self) -> ShellArgs:
        if bool(self.command) == bool(self.argv):
            raise ValueError("give exactly one of 'command' or 'argv'")
        if any(len(host) > 253 for host in self.egress):
            raise ValueError("a host name is too long")
        return self


def write_risk(path: str, append: bool, overwrite: bool) -> str:
    """A new plain-data file is medium; replacing or extending a file, or any write of a
    STRUCTURED format (JSON, YAML) is high (asked on the device, never
    remembered) - on the core's side and on the node's alike."""
    return "high" if append or overwrite or structured(path) else "medium"


def write_text_refusal(path: str, text: str, *, append: bool) -> str | None:
    """The checks a write's text gets before anyone is asked - in the node's gate and on the
    core - and again when it runs: JSON/YAML is written whole (an append would check only a
    piece), must parse plainly and stay under `MAX_STRUCTURED_BYTES`; Markdown carries no code a
    note app would run."""
    if append and structured(path):
        return "append_refused"
    return content_refusal(path, text)


def write_summary(path: str, text: str, *, append: bool, overwrite: bool) -> dict[str, Any]:
    """What a person decides on for a write - the same fields in the core's permission target
    and in the device's dialog: where, how, how much (bytes, lines, a SHA-256 to compare), and
    the content - in full when it is short, otherwise its first and last `PREVIEW_CHARS`
    characters with an explicit "content shortened - N characters not shown"."""
    data = text.encode("utf-8", errors="replace")
    shown: dict[str, Any] = {
        "path": path,
        "mode": "append" if append else "overwrite" if overwrite else "new file",
        "bytes": len(data),
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }
    if len(text) <= 2 * PREVIEW_CHARS:
        shown["content"] = text
    else:
        hidden = len(text) - 2 * PREVIEW_CHARS
        shown["content_start"] = text[:PREVIEW_CHARS]
        shown["content_end"] = text[-PREVIEW_CHARS:]
        shown["not_shown"] = f"content shortened - {hidden} characters not shown"
    return shown


def shell_program() -> tuple[str, ...]:
    """The OS shell for a `command` line, by absolute path (never one found on PATH)."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return (ps, "-NoProfile", "-NonInteractive", "-Command")
    return ("/bin/sh", "-c")
