"""The entry points for listing, reading and writing: a path is opened on the folder's handle, the
verdict is taken on that handle (see the package docstring)."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterator
from typing import Any

from nox.node.device.safe_fs.core import _NOCTTY, _NONBLOCK, _WINDOWS, OutsideFolders
from nox.node.device.safe_fs.folders import Folders, _Root
from nox.node.device.safe_fs.formats import execution_trigger, write_refusal
from nox.node.device.safe_fs.posix import _posix_open
from nox.node.device.safe_fs.windows import _win_final, _win_open
from nox.node.device.safe_fs.workspace import marked_above


def open_existing(folders: Folders, text: str, *, write: bool = False) -> int:
    """An fd for an existing file (or folder, read-only), checked on the handle."""
    root, parts = folders.split(text)
    if write:
        _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_open(folders, root, parts, write=write, create=False)
    # Read and write: an append reads what is there through the same handle (`hands._write`).
    flags = (os.O_RDWR if write else os.O_RDONLY) | _NONBLOCK | _NOCTTY
    return _posix_open(folders, root, parts, flags, write=write, create=False)


def open_new(folders: Folders, text: str) -> int:
    """An fd for a file that must not exist yet (never follows a link at its name)."""
    root, parts = folders.split(text)
    if not parts:
        raise OutsideFolders("invalid_path", "a folder cannot be written as a file")
    _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_open(folders, root, parts, write=True, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NONBLOCK | _NOCTTY
    return _posix_open(folders, root, parts, flags, write=True, create=True)


def _lexical_write_check(root: _Root, parts: list[str]) -> None:
    refusal = write_refusal(parts)
    if refusal is not None:
        raise OutsideFolders(refusal, "only data files are written (allowlist)")
    marked = marked_above(os.path.join(root.path, *parts[:-1]))
    if marked is not None:
        raise OutsideFolders(marked, "nothing in a repository or app workspace is written")
    if execution_trigger(os.path.join(root.path, *parts)):
        raise OutsideFolders("execution_trigger", "writing there could make something run")


@contextlib.contextmanager
def list_folder(folders: Folders, text: str) -> Iterator[Iterator[os.DirEntry[str]]]:
    """The entries of one allowed folder, enumerated through (POSIX) or under (Windows) its
    verified handle. Entries are never followed: a link is reported as one."""
    fd = open_existing(folders, text)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OutsideFolders("not_a_folder", "that is a file, not a folder")
        if not _WINDOWS:
            scan: Any = os.scandir  # by fd: POSIX only (typeshed says so per platform)
            with scan(fd) as entries:
                yield entries
            return
        final = _win_final(fd)
        before = os.fstat(fd).st_ino
        if final is None or os.stat(final).st_ino != before:
            raise OutsideFolders("changed", "the folder changed while it was opened")
        with os.scandir(final) as entries:
            yield entries
        if os.stat(final).st_ino != before:
            raise OutsideFolders("changed", "the folder changed while it was listed")
    finally:
        os.close(fd)
