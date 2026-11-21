"""Moving and deleting on Windows, relative to verified handles: the rename without replacing and
the Recycle Bin. The Win32 library is loaded inside the functions, so the module also imports on
POSIX."""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import re
import stat
from pathlib import Path
from typing import Any

from nox.node.device.safe_fs.core import MAX_TRASH_BYTES, Moved, OutsideFolders, _norm
from nox.node.device.safe_fs.folders import Folders, _Root
from nox.node.device.safe_fs.text_checks import _target_content_refusal
from nox.node.device.safe_fs.windows import (
    _DELETE,
    _FILE_FLAG_BACKUP_SEMANTICS,
    _FILE_SHARE_READ,
    _FILE_SHARE_WRITE,
    _FILE_TYPE_DISK,
    _GENERIC_READ,
    _OPEN_EXISTING,
    _kernel32,
    _msvcrt,
    _win_final,
)

_FILE_SHARE_DELETE = 0x4


_FILE_READ_ATTRIBUTES = 0x80


_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


_FILE_RENAME_INFO = 3


_FILE_STANDARD_INFO = 1


_DRIVE_FIXED = 3


@functools.cache
def _kernel32_more() -> Any:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    k32 = _kernel32()
    k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    k32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.GetVolumePathNameW.restype = wintypes.BOOL
    k32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    k32.GetVolumeNameForVolumeMountPointW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    return k32


def _win_fd(path: str, access: int, share: int, flags: int) -> int:
    """An fd for an existing file or folder on a local disk (`CreateFileW`, long-path form)."""
    import ctypes  # noqa: PLC0415

    winapi: Any = ctypes
    k32 = _kernel32()
    handle = k32.CreateFileW(
        path if path.startswith("\\\\?\\") else "\\\\?\\" + path,
        access,
        share,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise winapi.WinError(winapi.get_last_error())
    try:
        if k32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OutsideFolders("not_a_file", "only files on a disk are opened")
        fd: int = _msvcrt().open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        k32.CloseHandle(handle)
        raise
    return fd


def _win_leaf(folders: Folders, root: _Root, path: str, access: int, share: int) -> tuple[int, str]:
    """The file itself - never a link it may be (`FILE_FLAG_OPEN_REPARSE_POINT`) -, checked on
    its handle with the write rules. The fd and its real path."""
    fd = _win_fd(path, access, share, _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT)
    try:
        info = os.fstat(fd)
        if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OutsideFolders("link_refused", "links are not followed on this device")
        if stat.S_ISDIR(info.st_mode):
            raise OutsideFolders("not_a_file", "folders are not moved or deleted, only files")
        final = _win_final(fd)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        folders._verify(root, info, final, write=True)
    except BaseException:
        os.close(fd)
        raise
    return fd, final


def _win_folder(folders: Folders, root: _Root, path: str) -> tuple[int, str]:
    """A folder, pinned: open without `FILE_SHARE_DELETE` (it cannot be renamed or replaced
    while held), verified on its handle. The fd and its real path."""
    fd = _win_fd(
        path, _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE, _FILE_FLAG_BACKUP_SEMANTICS
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", path)
        final = _win_final(fd)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        folders._verify(root, info, final)
    except BaseException:
        os.close(fd)
        raise
    return fd, final


def _win_rename(fd: int, new_path: str) -> None:
    """`SetFileInformationByHandle(FileRenameInfo)` on this handle, never replacing a file."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    winapi: Any = ctypes
    name = new_path if new_path.startswith("\\\\?\\") else "\\\\?\\" + new_path

    class RenameInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_wchar * (len(name) + 1)),
        )

    info = RenameInfo(0, None, len(name) * 2, name)
    handle = _msvcrt().get_osfhandle(fd)
    if not _kernel32().SetFileInformationByHandle(
        handle, _FILE_RENAME_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise winapi.WinError(winapi.get_last_error())


def _win_move(
    folders: Folders, sroot: _Root, sparts: list[str], troot: _Root, tparts: list[str]
) -> Moved:
    folder_fd, folder = _win_folder(folders, troot, os.path.join(troot.path, *tparts[:-1]))
    try:
        target = os.path.join(folder, tparts[-1])
        folders._verify_name(troot, target)
        source_fd, source = _win_leaf(
            folders,
            sroot,
            os.path.join(sroot.path, *sparts),
            _GENERIC_READ | _DELETE,  # DELETE: renamed through this very handle
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # no FILE_SHARE_DELETE: nobody else renames it
        )
        try:
            if _norm(source) == _norm(target):
                raise FileExistsError(errno.EEXIST, "the target exists", target)
            if os.fstat(source_fd).st_dev != os.fstat(folder_fd).st_dev:
                raise OutsideFolders("cross_device", "a file is moved only within one drive")
            _target_content_refusal(
                lambda n: os.read(source_fd, n), os.fstat(source_fd).st_size, tparts[-1]
            )
            _win_rename(source_fd, target)
            now = _win_final(source_fd)
            try:
                if now is None or _norm(now) != _norm(target):
                    raise OutsideFolders("changed", "the file did not arrive where it should")
                folders._verify(troot, os.fstat(source_fd), now, write=True)
            except OutsideFolders:
                with contextlib.suppress(OSError):
                    _win_rename(source_fd, source)  # back where it was
                raise
            return Moved(source, now)
        finally:
            os.close(source_fd)
    finally:
        os.close(folder_fd)


def _win_volume(path: str) -> tuple[str, str]:
    """The volume root of `path` (`D:\\`) and its GUID (`{...}`, empty when unknown)."""
    import ctypes  # noqa: PLC0415

    k32 = _kernel32_more()
    buffer = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(path, buffer, 1024):
        return "", ""
    root = buffer.value
    guid = ctypes.create_unicode_buffer(128)
    if not k32.GetVolumeNameForVolumeMountPointW(root, guid, 128):
        return root, ""
    found = re.search(r"\{[0-9A-Fa-f-]{36}\}", guid.value)
    return root, found.group(0) if found else ""


def _win_bin_refusal(final: str, size: int) -> str | None:
    """Why the Recycle Bin would not keep this file (Windows would delete it for good), or None:
    not a fixed drive, "delete immediately" (`NoRecycleFiles` policy, `NukeOnDelete`), or larger
    than the bin's size (`MaxCapacity`, MB) - read like Explorer reads them."""
    root, guid = _win_volume(final)
    if not root or _kernel32_more().GetDriveTypeW(root) != _DRIVE_FIXED:
        return "the drive has no Recycle Bin"
    winreg: Any = __import__("winreg")

    def value(hive: Any, key: str, name: str) -> int | None:
        try:
            with winreg.OpenKey(hive, key) as handle:
                found, _kind = winreg.QueryValueEx(handle, name)
        except OSError:
            return None
        return int(found) if isinstance(found, int) else None

    policy = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        if value(hive, policy, "NoRecycleFiles") == 1:
            return "the Recycle Bin is switched off by policy"
    if guid:
        volume = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume\{guid}"
        if value(winreg.HKEY_CURRENT_USER, volume, "NukeOnDelete") == 1:
            return "the Recycle Bin of this drive deletes files immediately"
        capacity = value(winreg.HKEY_CURRENT_USER, volume, "MaxCapacity")
        if capacity is not None and size > capacity * 1024 * 1024:
            return "the file is larger than this drive's Recycle Bin"
    return None


def _win_delete_pending(fd: int) -> bool:
    """`FILE_STANDARD_INFO.DeletePending` of this handle (True when it cannot be read)."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class StandardInfo(ctypes.Structure):
        _fields_ = (
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        )

    info = StandardInfo()
    handle = _msvcrt().get_osfhandle(fd)
    if not _kernel32_more().GetFileInformationByHandleEx(
        handle, _FILE_STANDARD_INFO, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return True
    return bool(info.DeletePending)


def _win_trash(folders: Folders, root: _Root, parts: list[str]) -> str:
    from nox.files.recycle import RecycleError, to_recycle_bin  # noqa: PLC0415

    folder_fd, folder = _win_folder(folders, root, os.path.join(root.path, *parts[:-1]))
    try:
        fd, final = _win_leaf(
            folders,
            root,
            os.path.join(root.path, *parts),
            # Attributes only, every sharing mode: this handle takes part in no sharing check,
            # so the shell can move the file into the bin - and it shows where the file went.
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        )
        try:
            if _norm(os.path.dirname(final)) != _norm(folder):
                raise OutsideFolders("changed", "the file changed while it was opened")
            size = os.fstat(fd).st_size
            if size > MAX_TRASH_BYTES:
                raise OutsideFolders("too_large", "a file this large is not put in the trash here")
            refusal = _win_bin_refusal(final, size)
            if refusal is not None:
                raise OutsideFolders("trash_unavailable", refusal)
            failure = ""
            try:
                to_recycle_bin(Path(final))
            except RecycleError as exc:
                failure = str(exc)
            if _win_delete_pending(fd):
                raise OutsideFolders(
                    "trash_failed", "Windows deleted the file instead of recycling"
                )
            now = _win_final(fd)
            if now is None or "\\$RECYCLE.BIN\\" not in now.upper():
                if failure:
                    raise OutsideFolders("trash_failed", failure[:200])
                raise OutsideFolders("changed", "something else went to the Recycle Bin")
            return now
        finally:
            os.close(fd)
    finally:
        os.close(folder_fd)
