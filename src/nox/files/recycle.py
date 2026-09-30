"""Deleting a file the way a person deletes a file: into the Recycle Bin.

`os.unlink` is the obvious implementation and the wrong one. A language model asking to tidy a
folder will occasionally be wrong about which file, and the difference between a mistake that can be
undone and one that cannot is the difference between an annoyance and a loss. So the only delete Nox
has goes through the Windows shell with `FOF_ALLOWUNDO`, and if the shell call is unavailable or
fails there is no fallback - the file stays.

`SHFileOperationW` is the API that puts things in the bin; `DeleteFile` and `os.unlink` do not,
whatever flags they are given. Its `pFrom` is a *double* null-terminated list, which is the detail
that silently deletes nothing when it is missed.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["RecycleError", "available", "to_recycle_bin"]

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
#: Undo is the whole point; the rest keeps a background task from opening shell dialogs nobody
#: asked for and nobody would see.
FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI


class RecycleError(Exception):
    """The shell refused the delete. The file is still there."""


if sys.platform == "win32":

    class _ShFileOpStruct(ctypes.Structure):
        _fields_ = (
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", ctypes.c_void_p),
            ("pTo", ctypes.c_void_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        )


def available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def to_recycle_bin(path: Path) -> None:
    """Move one existing file or folder to the Recycle Bin. Raises `RecycleError` on any failure."""
    if not available():  # pragma: no cover - the target platform is Windows
        raise RecycleError("the Recycle Bin is only available on Windows")
    if not path.exists():
        raise RecycleError(f"{path} does not exist")

    # Double null terminator: the API reads pFrom as a list of paths and stops at an empty one.
    buffer = ctypes.create_unicode_buffer(f"{path}\0")
    operation = _ShFileOpStruct(
        hwnd=None,
        wFunc=FO_DELETE,
        pFrom=ctypes.cast(buffer, ctypes.c_void_p),
        pTo=None,
        fFlags=FLAGS,
        fAnyOperationsAborted=False,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0:
        raise RecycleError(f"the shell refused to delete {path} (SHFileOperation returned {code})")
    if operation.fAnyOperationsAborted:
        raise RecycleError(f"the delete of {path} was aborted")
    if path.exists():
        # It returned success and the file is still there. Report that rather than the return code.
        raise RecycleError(f"{path} is still there after the shell reported success")
    log.info("files.recycled", path=str(path))
