"""The pieces every part of `safe_fs` shares: the platform switch, the refusal (`OutsideFolders`),
the path spellings the walk compares with, the result of a move and the largest file the trash
takes."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_WINDOWS = sys.platform == "win32"


#: A FIFO opened for reading would block; with `O_NONBLOCK` it fails or returns at once.
_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


#: A terminal device never becomes the node's controlling terminal.
_NOCTTY: int = getattr(os, "O_NOCTTY", 0)


class OutsideFolders(Exception):  # noqa: N818 - a refusal, named for what it says
    """The path is not one this device lets Nox open. `reason` is a short machine token."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _plain(path: str) -> str:
    """A resolved Windows path without the long-path prefix, the way the answers of
    `GetFinalPathNameByHandleW` are compared (`\\\\?\\C:\\x` -> `C:\\x`)."""
    return path[4:] if _WINDOWS and path.startswith("\\\\?\\") and path[5:6] == ":" else path


def _norm(path: str) -> str:
    text = path.replace("/", "\\") if _WINDOWS else path
    return os.path.normcase(text.rstrip("\\/")) or text


def _within(path: str, folder: str) -> bool:
    inner, outer = _norm(path), _norm(folder)
    sep = "\\" if _WINDOWS else "/"
    return inner == outer or inner.startswith(outer.rstrip(sep) + sep)


def _peek(name: str, dir_fd: int) -> os.stat_result | None:
    """The entry as it is (`fstatat` without following), None when there is none."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None


#: The largest file a node puts in the trash: far above any note or log, far below what a Recycle
#: Bin holds - a file too large for the bin would be deleted for good by Windows, never that.
MAX_TRASH_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class Moved:
    """Where a moved file was and where it is now (the real paths)."""

    source: str
    target: str
