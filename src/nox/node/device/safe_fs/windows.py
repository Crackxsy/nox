"""Opening on Windows: `CreateFileW` without `FILE_SHARE_DELETE`, then the path the open really
reached (`GetFinalPathNameByHandleW`). The Win32 library is loaded inside the functions, so the
module also imports on POSIX."""

from __future__ import annotations

import functools
import os
from typing import Any

from nox.node.device.safe_fs.core import OutsideFolders
from nox.node.device.safe_fs.folders import Folders, _Root

_GENERIC_READ = 0x80000000


_GENERIC_WRITE = 0x40000000


_DELETE = 0x00010000


_FILE_SHARE_READ = 0x1


_FILE_SHARE_WRITE = 0x2


_CREATE_NEW = 1


_OPEN_EXISTING = 3


_FILE_ATTRIBUTE_NORMAL = 0x80


_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


_FILE_DISPOSITION_INFO = 4


_FILE_TYPE_DISK = 1


@functools.cache
def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    loader: Any = ctypes
    k32 = loader.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    k32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    k32.SetFileInformationByHandle.restype = wintypes.BOOL
    k32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetFileType.restype = wintypes.DWORD
    k32.GetFileType.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _win_final_handle(k32: Any, handle: int) -> str | None:
    """`GetFinalPathNameByHandleW` (normalized, DOS volume name): the real path of a handle,
    without the `\\\\?\\` prefix. A network path (`\\\\?\\UNC\\...`) is None (refused)."""
    import ctypes

    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = k32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            return None
        if length < size:
            break
        size = length + 1
    text = buffer.value
    if text.upper().startswith("\\\\?\\UNC\\"):
        return None
    return text[4:] if text.startswith("\\\\?\\") else text


def _msvcrt() -> Any:
    import importlib

    return importlib.import_module("msvcrt")


def _win_final(fd: int) -> str | None:
    return _win_final_handle(_kernel32(), _msvcrt().get_osfhandle(fd))


def _win_open(folders: Folders, root: _Root, parts: list[str], *, write: bool, create: bool) -> int:
    import ctypes

    msvcrt = _msvcrt()
    winapi: Any = ctypes
    k32 = _kernel32()
    path = os.path.join(root.path, *parts)
    access = (_GENERIC_WRITE | _GENERIC_READ) if write else _GENERIC_READ
    if create:
        access |= _DELETE  # to take a wrongly placed new file back through this same handle
    # The long-path form: no further normalisation by Win32 (the parts were checked above).
    handle = k32.CreateFileW(
        path if path.startswith("\\\\?\\") else "\\\\?\\" + path,
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # never FILE_SHARE_DELETE: no rename while open
        None,
        _CREATE_NEW if create else _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise winapi.WinError(winapi.get_last_error())

    def take_back(raw: int) -> None:
        flag = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile = TRUE
        k32.SetFileInformationByHandle(raw, _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1)

    try:
        if k32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OutsideFolders("not_a_file", "only files on a disk are opened")
        final = _win_final_handle(k32, handle)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        fd: int = msvcrt.open_osfhandle(handle, os.O_RDWR if write else os.O_RDONLY)
    except BaseException:
        if create:
            take_back(handle)
        k32.CloseHandle(handle)
        raise
    try:
        folders._verify(root, os.fstat(fd), final, write=write)
    except BaseException:
        if create:
            take_back(msvcrt.get_osfhandle(fd))
        os.close(fd)
        raise
    return fd
