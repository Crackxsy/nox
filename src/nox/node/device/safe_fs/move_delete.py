"""`file.move` and `file.delete`: both ends of a move and the file to delete are opened like any
other path, then acted on relative to the verified handles - a rename without replacing, into
the trash only (never an unlink)."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nox.node.device.safe_fs.core import _WINDOWS, MAX_TRASH_BYTES, Moved, OutsideFolders, _peek
from nox.node.device.safe_fs.folders import Folders
from nox.node.device.safe_fs.formats import _class_refusal
from nox.node.device.safe_fs.opening import _lexical_write_check, open_existing
from nox.node.device.safe_fs.posix import (
    _final_path,
    _posix_leaf,
    _posix_move,
    _posix_walk,
    _rename_noreplace,
)
from nox.node.device.safe_fs.windows_files import _win_move, _win_trash


def move_file(folders: Folders, source: str, target: str) -> Moved:
    """Move or rename one regular file inside the allowed folders.

    Both ends follow the write rules (allowlisted data formats only - `notes.txt` never becomes
    `run.bat` -, no hidden names, no working tree or app workspace, no execution trigger, never a
    protected place). A target that is an existing folder gets the file's own name; otherwise the
    target must not exist (a move never replaces anything) and its folder must (folders are never
    created). Links are refused, never followed, at both ends; folders are not moved; a move never
    crosses drives (no copy-and-delete).

    Bound to handles: on POSIX both folders are walked and verified as directory fds and the rename
    is relative to them without replacing (`renameat2(RENAME_NOREPLACE)`,
    `renameatx_np(RENAME_EXCL)`, else `linkat` + `unlinkat`); on Windows the verified source handle
    (held without `FILE_SHARE_DELETE`, so nobody else renames it meanwhile) is renamed into the
    verified target folder, which stays open meanwhile. The moved file is checked again
    afterwards: anything else in its place is moved back and the call refused (`changed`).

    **A file keeps its kind.** Plain text (`.txt`, `.csv`, ...), Markdown, JSON and YAML get
    different content checks when written, so a move refuses any change from one of these kinds
    to another (`extension_class_changed`, both directions: `a.txt` -> `a.md` and `a.md` ->
    `a.txt`); renaming within a kind is fine. And the file's text - read through the verified
    handle, bounded by `MAX_TEXT_BYTES` - must pass the target name's checks too."""
    sroot, sparts = folders.split(source)
    troot, tparts = folders.split(target)
    if not sparts or not tparts:
        raise OutsideFolders("invalid_path", "a folder is not moved; name a file")
    if _is_folder(folders, target):
        tparts = [*tparts, sparts[-1]]
    _class_refusal(sparts[-1], tparts[-1])
    _lexical_write_check(sroot, sparts)
    _lexical_write_check(troot, tparts)
    if _WINDOWS:
        return _win_move(folders, sroot, sparts, troot, tparts)
    return _posix_move(folders, sroot, sparts, troot, tparts)


def trash_support() -> str | None:
    """Why `file.delete` cannot work on this device, or None (looked at without deleting)."""
    if sys.platform == "darwin":
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec("Foundation") is None:
            return "the Trash needs PyObjC on a Mac (extra `shell`)"
    return None


def trash_file(folders: Folders, text: str) -> str:
    """Move one regular file into this user's trash - the Recycle Bin on Windows, the Trash on a
    Mac, the freedesktop trash (`~/.local/share/Trash`) on Linux - and return where it went. Never
    an unlink: where the trash cannot take the file (another drive than the user's trash on
    Linux, a removable drive or "delete immediately" on Windows, a file over `MAX_TRASH_BYTES`),
    it is refused (`trash_unavailable`) and the file stays.

    The same rules as a write (allowlisted data formats, no working tree or app workspace, no
    execution trigger, no protected place, no link, no second hard link). On POSIX the file is
    renamed relative to its verified directory fd into the trash (Linux: with its `.trashinfo`
    written first, as the specification says), on a Mac by the system's `trashItemAtURL`; on
    Windows the parent folder is pinned (held open without `FILE_SHARE_DELETE`) while the shell
    recycles the file, and the file's own handle - open the whole time - must afterwards be in
    the Recycle Bin. In every case the trashed item must be the file that was checked
    (`changed` otherwise)."""
    root, parts = folders.split(text)
    if not parts:
        raise OutsideFolders("invalid_path", "a folder is not deleted; name a file")
    _lexical_write_check(root, parts)
    if _WINDOWS:
        return _win_trash(folders, root, parts)
    directory = _posix_walk(folders, root, parts[:-1], write=True)
    try:
        identity, size = _posix_leaf(folders, root, directory, parts[-1])
        if size > MAX_TRASH_BYTES:
            raise OutsideFolders("too_large", "a file this large is not put in the trash here")
        folder = _final_path(directory)
        if folder is None:
            raise OutsideFolders("trash_unavailable", "where the file lies cannot be told here")
        original = os.path.join(folder, parts[-1])
        if sys.platform == "darwin":
            return _mac_trash(original, identity)
        return _xdg_trash(directory, parts[-1], identity, original)
    finally:
        os.close(directory)


def _is_folder(folders: Folders, text: str) -> bool:
    """Whether `text` names an existing folder (checked on its handle, never followed)."""
    try:
        fd = open_existing(folders, text)
    except (OSError, OutsideFolders):
        return False
    try:
        return stat.S_ISDIR(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def _trash_names(name: str) -> Iterator[str]:
    """`name`, then `name.2`, `name.3`, ... (before the extension) - a free name in the trash."""
    yield name
    stem, dot, ext = name.rpartition(".")
    if not stem:
        stem, dot, ext = name, "", ""
    for number in range(2, 1000):
        yield f"{stem}.{number}{dot}{ext}"


def _xdg_trash(directory: int, name: str, identity: tuple[int, int], original: str) -> str:
    """The freedesktop trash in this user's data folder: `info/<name>.trashinfo` is created
    exclusively first (it reserves the name), then the file is renamed into `files/` relative to
    both verified folders. Only on the drive the trash is on - a file on another drive is
    refused, never copied."""
    from urllib.parse import quote  # noqa: PLC0415

    data = os.environ.get("XDG_DATA_HOME") or ""
    base = Path(data) if os.path.isabs(data) else Path.home() / ".local" / "share"
    trash = base / "Trash"
    nofollow, cloexec = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_CLOEXEC", 0)
    folder_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    try:
        trash.mkdir(mode=0o700, parents=True, exist_ok=True)
        trash_fd = os.open(trash, folder_flags)
    except OSError as exc:
        raise OutsideFolders("trash_unavailable", "this user's trash cannot be used") from exc
    opened: list[int] = []
    try:
        for sub in ("files", "info"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(sub, 0o700, dir_fd=trash_fd)
            try:
                opened.append(os.open(sub, folder_flags, dir_fd=trash_fd))
            except OSError as exc:
                raise OutsideFolders("trash_unavailable", "the trash is not a folder") from exc
        files, info = opened
        if os.fstat(files).st_dev != identity[0]:
            raise OutsideFolders(
                "trash_unavailable", "the file is on another drive than this user's trash"
            )
        exclusive = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec
        for candidate in _trash_names(name):
            try:
                record = os.open(f"{candidate}.trashinfo", exclusive, 0o600, dir_fd=info)
            except FileExistsError:
                continue
            if _peek(candidate, files) is None:
                break
            os.close(record)
            os.unlink(f"{candidate}.trashinfo", dir_fd=info)
        else:
            raise OutsideFolders("trash_unavailable", "no free name in the trash")
        when = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with os.fdopen(record, "w", encoding="utf-8") as handle:
                handle.write(f"[Trash Info]\nPath={quote(original)}\nDeletionDate={when}\n")
            _rename_noreplace(directory, name, files, candidate)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(f"{candidate}.trashinfo", dir_fd=info)
            raise
        moved = os.stat(candidate, dir_fd=files, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != identity:
            with contextlib.suppress(OSError):
                _rename_noreplace(files, candidate, directory, name)
                os.unlink(f"{candidate}.trashinfo", dir_fd=info)
            raise OutsideFolders("changed", "the file changed while it was deleted (put back)")
        return str(trash / "files" / candidate)
    finally:
        for fd in opened:
            os.close(fd)
        os.close(trash_fd)


def _mac_trash(original: str, identity: tuple[int, int]) -> str:
    """The system's own Trash (`NSFileManager.trashItemAtURL`, "Put Back" works); the item it
    moved must be the file that was checked."""
    try:
        foundation: Any = __import__("Foundation")
    except ImportError as exc:
        raise OutsideFolders("trash_unavailable", "the Trash needs PyObjC on a Mac") from exc
    url = foundation.NSURL.fileURLWithPath_(original)
    manager = foundation.NSFileManager.defaultManager()
    ok, result, _error = manager.trashItemAtURL_resultingItemURL_error_(url, None, None)
    if not ok or result is None:
        raise OutsideFolders("trash_unavailable", "macOS did not move the file to the Trash")
    where = str(result.path())
    moved = os.lstat(where)
    if (moved.st_dev, moved.st_ino) != identity:
        raise OutsideFolders("changed", f"something else went to the Trash ({where})")
    return where
