"""Opening a path for a node's `file.*` tools: inside a folder this device allows, decided on the
real file at the moment it is opened - never on the text the core sent.

Order for every open (list, read, write):

1. **The text** (`split`): UNC and Win32 device paths, `..`, a leading `~`, 8.3 short names
   (`NOXNOD~1`), NUL, alternate data streams (`a.txt:s`), Windows device names (`CON`, `aux.txt`)
   and names Windows would silently change (a trailing dot or space) are refused before anything
   touches the file system - an SMB path is never resolved, so the device never reaches out. A
   relative path is relative to the first allowed folder; an absolute one must spell one of them.
2. **The open, bound to a handle**:
   * POSIX - `openat`-style walk from the allowed folder (whose identity was recorded when the
     folders were built): every step `O_NOFOLLOW` (+ `O_DIRECTORY`), so no symlink anywhere below
     the folder is followed, and every directory passed is compared with the protected folders by
     `(st_dev, st_ino)` - a bind mount or a renamed copy does not change that.
   * Windows - `CreateFileW` without `FILE_SHARE_DELETE` (the file cannot be renamed away while it
     is open), then `GetFinalPathNameByHandleW` on that handle: the path the open really reached
     after junctions, symlinks and 8.3 names. A new file is created with `CREATE_NEW` and, when its
     final path is not allowed, deleted again through the same handle; an existing one is
     truncated only after the check (never `O_TRUNC` at open).
3. **The verdict on the handle** (`_verify`): inside an allowed folder, not inside a protected
   one (the node's own folder with its key, certificates and audit trail; the keyring's folders),
   not a hard link to a protected file, a regular file or a directory (no FIFO, socket, device).

Everything read or written afterwards goes through that one handle, so swapping the path between
the check and the use changes nothing (`test_c5.py` swaps it). What the OS cannot bind to a handle
is said plainly: listing a folder on Windows enumerates by its verified path while the folder's
handle is held open and compares the folder's identity before and after.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import stat
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nox.node.gate import is_remote_or_device_path, names_a_path_alias

__all__ = [
    "Folders",
    "OutsideFolders",
    "keyring_folders",
    "list_folder",
    "open_existing",
    "open_new",
]

_WINDOWS = sys.platform == "win32"
#: Win32 device names, with or without an extension (`nul.txt` is still NUL on most Windows).
_DEVICE_NAME = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I
)
_MAX_PROTECTED_ENTRIES = 512
#: Opening a FIFO for reading would block; `O_NONBLOCK` makes it fail or return at once.
_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


class OutsideFolders(Exception):  # noqa: N818 - a refusal, named for what it says
    """The path is not one this device lets Nox open. `reason` is a short machine token."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _norm(path: str) -> str:
    text = path.replace("/", "\\") if _WINDOWS else path
    return os.path.normcase(text.rstrip("\\/")) or text


def _plain(path: str) -> str:
    """A resolved Windows path without the long-path prefix, the way the answers of
    `GetFinalPathNameByHandleW` are compared (`\\\\?\\C:\\x` -> `C:\\x`)."""
    return path[4:] if _WINDOWS and path.startswith("\\\\?\\") and path[5:6] == ":" else path


def _within(path: str, folder: str) -> bool:
    inner, outer = _norm(path), _norm(folder)
    sep = "\\" if _WINDOWS else "/"
    return inner == outer or inner.startswith(outer.rstrip(sep) + sep)


def keyring_folders() -> list[Path]:
    """Where this user's credential stores keep their files: never reachable by a file tool."""
    home = Path.home()
    if _WINDOWS:
        found = []
        for variable in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(variable, "")
            if base:
                found += [
                    Path(base) / "Microsoft" / "Credentials",
                    Path(base) / "Microsoft" / "Protect",
                ]
        return found
    if sys.platform == "darwin":
        return [home / "Library" / "Keychains"]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [data / "keyrings", data / "python_keyring", home / ".gnupg"]


@dataclass(frozen=True)
class _Root:
    path: str
    #: (st_dev, st_ino) when the folder was built: a folder swapped for another is refused.
    identity: tuple[int, int]


@dataclass(frozen=True)
class Folders:
    """The folders this device allows (`node.yaml: folders`, confirmed on the device) and the
    folders no tool may ever reach. Built per call from the confirmed manifest."""

    roots: tuple[_Root, ...]
    protected: tuple[str, ...]
    protected_ids: frozenset[tuple[int, int]]

    @classmethod
    def build(cls, roots: Iterable[str], protected: Iterable[Path]) -> Folders:
        guarded: list[str] = []
        ids: set[tuple[int, int]] = set()
        for folder in protected:
            try:
                real = _plain(os.path.realpath(folder))
                info = os.stat(real)
            except (OSError, ValueError):
                guarded.append(os.path.abspath(folder))  # not there yet: still never inside it
                continue
            guarded.append(real)
            ids.add((info.st_dev, info.st_ino))
            ids |= _identities(Path(real))
        allowed: list[_Root] = []
        for entry in roots:
            text = str(entry).strip()
            if not text or is_remote_or_device_path(text) or not os.path.isabs(text):
                continue
            try:
                real = _plain(os.path.realpath(text))
                info = os.stat(real)
            except (OSError, ValueError):
                continue  # an unplugged drive is not an allowed folder right now
            if not stat.S_ISDIR(info.st_mode) or any(_within(real, g) for g in guarded):
                continue  # a folder inside the node's own (or the keyring's) never counts
            allowed.append(_Root(real, (info.st_dev, info.st_ino)))
        return cls(tuple(allowed), tuple(guarded), frozenset(ids))

    # -- 1. the text ---------------------------------------------------------------------------

    def split(self, text: str) -> tuple[_Root, list[str]]:
        """The allowed folder a path lies in and its parts below it, from the text alone."""
        if not self.roots:
            raise OutsideFolders("no_folders", "no folder is allowed for Nox on this device")
        raw = text.strip()
        if not raw or "\x00" in raw:
            raise OutsideFolders("invalid_path", "no usable path was given")
        if is_remote_or_device_path(raw) or names_a_path_alias(raw):
            raise OutsideFolders(
                "path_alias_refused", "network, device and alias paths are refused"
            )
        if os.path.isabs(raw) or (_WINDOWS and re.match(r"^[A-Za-z]:", raw)):
            if _WINDOWS and not re.match(r"^[A-Za-z]:[\\/]", raw):
                raise OutsideFolders("invalid_path", "a drive-relative path is refused")
            for root in self.roots:
                if _within(raw, root.path):
                    rest = raw[len(root.path.rstrip("\\/")) :]
                    return root, _parts(rest)
            raise OutsideFolders("outside_folders", "outside the folders allowed on this device")
        return self.roots[0], _parts(raw)

    # -- 3. the verdict on a handle --------------------------------------------------------------

    def _verify(self, root: _Root, info: os.stat_result, final: str | None) -> None:
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise OutsideFolders("not_a_file", "only regular files and folders are opened")
        if (info.st_dev, info.st_ino) in self.protected_ids:
            raise OutsideFolders("node_home_protected", "that file is protected on this device")
        if final is None:
            return
        if any(_within(final, g) for g in self.protected):
            raise OutsideFolders("node_home_protected", "that file is protected on this device")
        if not _within(final, root.path):
            raise OutsideFolders("outside_folders", "the path leads outside the allowed folders")


def _parts(rest: str) -> list[str]:
    parts = [p for p in re.split(r"[\\/]+", rest) if p not in ("", ".")]
    for part in parts:
        if part == "..":
            raise OutsideFolders("path_alias_refused", "`..` is refused")
        if _WINDOWS and (":" in part or part[-1] in ". " or _DEVICE_NAME.match(part)):
            raise OutsideFolders("invalid_path", "a stream, device or ambiguous name is refused")
    return parts


def _identities(folder: Path) -> set[tuple[int, int]]:
    """(st_dev, st_ino) of what lies in a protected folder: a hard link to one is refused."""
    found: set[tuple[int, int]] = set()
    with contextlib.suppress(OSError):
        for count, (base, dirs, files) in enumerate(os.walk(folder, followlinks=False)):
            for name in [*dirs, *files]:
                with contextlib.suppress(OSError):
                    info = os.stat(os.path.join(base, name), follow_symlinks=False)
                    found.add((info.st_dev, info.st_ino))
            if count > 8 or len(found) > _MAX_PROTECTED_ENTRIES:
                break
    return found


# -- 2. the open --------------------------------------------------------------------------------


def open_existing(folders: Folders, text: str, *, write: bool = False) -> int:
    """An fd for an existing file (or folder, read-only), checked on the handle."""
    root, parts = folders.split(text)
    if _WINDOWS:
        return _win_open(folders, root, parts, write=write, create=False)
    flags = (os.O_WRONLY if write else os.O_RDONLY) | _NONBLOCK
    return _posix_open(folders, root, parts, flags)


def open_new(folders: Folders, text: str) -> int:
    """An fd for a file that must not exist yet (never follows a link at its name)."""
    root, parts = folders.split(text)
    if not parts:
        raise OutsideFolders("invalid_path", "a folder cannot be written as a file")
    if _WINDOWS:
        return _win_open(folders, root, parts, write=True, create=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NONBLOCK
    return _posix_open(folders, root, parts, flags)


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


def _posix_open(folders: Folders, root: _Root, parts: list[str], flags: int) -> int:
    """Walk from the allowed folder without following any link; a new file is only its own
    entry (`O_CREAT | O_EXCL`), in a directory that was itself checked on the way."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root.path, os.O_RDONLY | directory | cloexec)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root.identity:
            raise OutsideFolders("changed", "the allowed folder was replaced")
        for part in parts[:-1]:
            try:
                step = os.open(part, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc, part, fd) from exc
            os.close(fd)
            fd = step
            folders._verify(root, os.fstat(fd), None)
        if not parts:
            final = os.dup(fd)
        else:
            try:
                final = os.open(parts[-1], flags | nofollow | cloexec, 0o600, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc, parts[-1], fd) from exc
    finally:
        os.close(fd)
    try:
        folders._verify(root, os.fstat(final), _proc_path(final))
    except BaseException:
        os.close(final)
        raise
    if flags & _NONBLOCK:
        os.set_blocking(final, True)
    return final


def _link_or(exc: OSError, name: str, dir_fd: int) -> Exception:
    """`O_NOFOLLOW` on a link answers ELOOP - or ENOTDIR for a directory step: then the entry is
    looked at (never followed) to say which it was."""
    import errno

    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return OutsideFolders("link_refused", "links are not followed on this device")
    if exc.errno == errno.ENOTDIR:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                return OutsideFolders("link_refused", "links are not followed on this device")
    return exc


def _proc_path(fd: int) -> str | None:
    """The kernel's own name for an fd (Linux), as an extra check next to the identities."""
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/self/fd/{fd}")
    return None


# -- Windows --------------------------------------------------------------------------------------

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
    try:
        if k32.GetFileType(handle) != _FILE_TYPE_DISK:
            raise OutsideFolders("not_a_file", "only files on a disk are opened")
        final = _win_final_handle(k32, handle)
        if final is None:
            raise OutsideFolders("outside_folders", "the path leads to a network or unknown place")
        fd: int = msvcrt.open_osfhandle(handle, os.O_RDWR if write else os.O_RDONLY)
    except BaseException:
        if create:
            flag = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile = TRUE
            k32.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1)
        k32.CloseHandle(handle)
        raise
    try:
        folders._verify(root, os.fstat(fd), final)
    except BaseException:
        if create:
            flag = ctypes.c_ubyte(1)
            k32.SetFileInformationByHandle(
                msvcrt.get_osfhandle(fd), _FILE_DISPOSITION_INFO, ctypes.byref(flag), 1
            )
        os.close(fd)
        raise
    return fd
