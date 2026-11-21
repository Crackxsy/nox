"""Opening, moving and walking on POSIX: an `openat`-style walk from the allowed folder, every
entry looked at without following it, and the rename that never replaces. Imports `fcntl` only
where it is used, so the module also imports on Windows."""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import stat
import sys
from typing import Any

from nox.node.device.safe_fs.core import _NOCTTY, _NONBLOCK, Moved, OutsideFolders, _peek
from nox.node.device.safe_fs.folders import Folders, _Root
from nox.node.device.safe_fs.text_checks import _target_content_refusal
from nox.node.device.safe_fs.workspace import _marked_fd


def _posix_open(
    folders: Folders, root: _Root, parts: list[str], flags: int, *, write: bool, create: bool
) -> int:
    """Walk from the allowed folder; look at every entry before opening it and never follow a
    link. A new file is only its own entry (`O_CREAT | O_EXCL`, mode 0600), in a directory that
    was itself checked on the way - and is removed from it again if its check fails."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    fd = _posix_walk(folders, root, parts[:-1], write=write)
    try:
        if not parts:
            final = os.dup(fd)
        else:
            seen = _peek(parts[-1], fd)
            if seen is not None:
                if create:
                    raise FileExistsError(errno.EEXIST, "the file exists", parts[-1])
                if stat.S_ISLNK(seen.st_mode):
                    raise OutsideFolders("link_refused", "links are not followed on this device")
                if not (stat.S_ISREG(seen.st_mode) or stat.S_ISDIR(seen.st_mode)):
                    raise OutsideFolders("not_a_file", "only regular files and folders are opened")
            elif not create:
                raise FileNotFoundError(errno.ENOENT, "no such file", parts[-1])
            try:
                final = os.open(parts[-1], flags | nofollow | cloexec, 0o600, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc) from exc
        try:
            folders._verify(root, os.fstat(final), _final_path(final), write=write)
        except BaseException:
            os.close(final)
            if create:
                with contextlib.suppress(OSError):
                    os.unlink(parts[-1], dir_fd=fd)
            raise
    finally:
        os.close(fd)
    if flags & _NONBLOCK:
        os.set_blocking(final, True)
    return final


def _posix_walk(folders: Folders, root: _Root, folder_parts: list[str], *, write: bool) -> int:
    """A directory fd for `root/folder_parts`, walked from the allowed folder (whose identity
    must not have changed): every entry is looked at without following it, opened `O_NOFOLLOW`
    relative to the one before, compared with the protected folders and - for a write - checked
    for a working tree or an app workspace."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    fd = os.open(root.path, os.O_RDONLY | directory | cloexec)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != root.identity:
            raise OutsideFolders("changed", "the allowed folder was replaced")
        if write:
            _marked_fd(fd)
        for part in folder_parts:
            seen = _peek(part, fd)
            if seen is None:
                raise FileNotFoundError(errno.ENOENT, "no such folder", part)
            if stat.S_ISLNK(seen.st_mode):
                raise OutsideFolders("link_refused", "links are not followed on this device")
            if not stat.S_ISDIR(seen.st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "not a folder", part)
            try:
                step = os.open(part, os.O_RDONLY | directory | nofollow | cloexec, dir_fd=fd)
            except OSError as exc:
                raise _link_or(exc) from exc
            os.close(fd)
            fd = step
            folders._verify(root, os.fstat(fd), None)
            if write:
                _marked_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _link_or(exc: OSError) -> Exception:
    """`O_NOFOLLOW` on a link that appeared after the look answers ELOOP."""
    if exc.errno in (errno.ELOOP, errno.EMLINK):
        return OutsideFolders("link_refused", "links are not followed on this device")
    return exc


def _final_path(fd: int) -> str | None:
    """The kernel's own name for an fd: `/proc/self/fd` on Linux, `F_GETPATH` on macOS."""
    if sys.platform == "darwin":
        import fcntl

        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is None:
            return None
        with contextlib.suppress(OSError):
            raw: bytes = fcntl.fcntl(fd, getpath, bytes(1024))
            return os.fsdecode(raw.split(b"\x00", 1)[0])
        return None
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/self/fd/{fd}")
    return None


#: `renameat2` (Linux) and `renameatx_np` (macOS): fail instead of replacing the target.
_RENAME_NOREPLACE = 1


_RENAME_EXCL = 0x4


def _posix_leaf(
    folders: Folders, root: _Root, directory: int, name: str, *, read_as: str | None = None
) -> tuple[tuple[int, int], int]:
    """The file `name` in a verified directory: looked at without following, opened `O_NOFOLLOW`
    and checked on its handle with the write rules. Its identity and size. With `read_as`, its
    text must also pass the checks of that (target) name."""
    seen = _peek(name, directory)
    if seen is None:
        raise FileNotFoundError(errno.ENOENT, "no such file", name)
    if stat.S_ISLNK(seen.st_mode):
        raise OutsideFolders("link_refused", "links are not followed on this device")
    if stat.S_ISDIR(seen.st_mode):
        raise OutsideFolders("not_a_file", "folders are not moved or deleted, only files")
    if not stat.S_ISREG(seen.st_mode):
        raise OutsideFolders("not_a_file", "only regular files are moved or deleted")
    flags = os.O_RDONLY | _NONBLOCK | _NOCTTY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags | getattr(os, "O_CLOEXEC", 0), dir_fd=directory)
    except OSError as exc:
        raise _link_or(exc) from exc
    try:
        info = os.fstat(fd)
        folders._verify(root, info, _final_path(fd), write=True)
        if read_as is not None:
            _target_content_refusal(lambda n: os.read(fd, n), info.st_size, read_as)
    finally:
        os.close(fd)
    identity = (info.st_dev, info.st_ino)
    if identity != (seen.st_dev, seen.st_ino):
        raise OutsideFolders("changed", "the file changed while it was opened")
    return identity, info.st_size


def _posix_move(
    folders: Folders, sroot: _Root, sparts: list[str], troot: _Root, tparts: list[str]
) -> Moved:
    source_dir = _posix_walk(folders, sroot, sparts[:-1], write=True)
    try:
        target_dir = _posix_walk(folders, troot, tparts[:-1], write=True)
        try:
            identity, _size = _posix_leaf(
                folders, sroot, source_dir, sparts[-1], read_as=tparts[-1]
            )
            if _peek(tparts[-1], target_dir) is not None:
                raise FileExistsError(errno.EEXIST, "the target exists", tparts[-1])
            if os.fstat(target_dir).st_dev != identity[0]:
                raise OutsideFolders("cross_device", "a file is moved only within one drive")
            source_folder, target_folder = _final_path(source_dir), _final_path(target_dir)
            if source_folder is None or target_folder is None:
                raise OutsideFolders("changed", "where the folders lie cannot be told here")
            target_final = os.path.join(target_folder, tparts[-1])
            folders._verify_name(troot, target_final)
            _rename_noreplace(source_dir, sparts[-1], target_dir, tparts[-1])
            moved = os.stat(tparts[-1], dir_fd=target_dir, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != identity:
                with contextlib.suppress(OSError):
                    _rename_noreplace(target_dir, tparts[-1], source_dir, sparts[-1])
                raise OutsideFolders("changed", "the file changed while it was moved (moved back)")
            return Moved(os.path.join(source_folder, sparts[-1]), target_final)
        finally:
            os.close(target_dir)
    finally:
        os.close(source_dir)


@functools.cache
def _libc() -> Any:
    import ctypes  # noqa: PLC0415

    loader: Any = ctypes
    if sys.platform == "darwin":
        return loader.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    return loader.CDLL(None, use_errno=True)


def _rename_noreplace(source_dir: int, source: str, target_dir: int, target: str) -> None:
    """Rename `source` (in `source_dir`) to `target` (in `target_dir`), failing with
    `FileExistsError` when the target exists - atomically where the system can (`renameat2`
    with `RENAME_NOREPLACE`, `renameatx_np` with `RENAME_EXCL`), else as a hard link (which
    fails on an existing name) followed by removing the old name."""
    import ctypes  # noqa: PLC0415

    function: Any = None
    flag = _RENAME_EXCL if sys.platform == "darwin" else _RENAME_NOREPLACE
    with contextlib.suppress(OSError, AttributeError):
        function = getattr(_libc(), "renameatx_np" if sys.platform == "darwin" else "renameat2")
    if function is not None:
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
        function.argtypes += [ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(source_dir, os.fsencode(source), target_dir, os.fsencode(target), flag) == 0:
            return
        code = ctypes.get_errno()
        unsupported = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", 0)}
        if code not in unsupported:
            raise OSError(code, os.strerror(code), target)
    os.link(source, target, src_dir_fd=source_dir, dst_dir_fd=target_dir, follow_symlinks=False)
    try:
        os.unlink(source, dir_fd=source_dir)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(target, dir_fd=target_dir)
        raise
