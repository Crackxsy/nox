"""The file operations themselves: list, read, write, move, delete.

Every one takes its path through `resolve_inside` first, so no code path in this module touches a
file without the boundary having agreed to it. The I/O runs off the event loop, because a read from
a sleeping external drive takes seconds and the pet has to keep breathing.

Three refusals are worth naming, because each one is a loss a model would otherwise cause casually:

* A write never overwrites unless the call says so. "Put this in notes.md" should not silently
  replace notes.md.
* A write never creates missing parent folders. A typo in a folder name would build a tree nobody
  asked for, in a place nobody looks.
* A read refuses a file that is not text. Feeding a JPEG into a prompt produces nonsense, costs a
  fortune and teaches the model nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nox.core.logging import get_logger
from nox.files.recycle import RecycleError, to_recycle_bin
from nox.files.roots import resolve_inside

log = get_logger(__name__)

__all__ = ["delete", "list_dir", "move", "read_text", "write_text"]

#: How much of the head is inspected for the "is this text" question. A text file that starts with
#: 8 KB of text and hides a null byte at the end is a text file for our purposes.
SNIFF_BYTES = 8192


def _stamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    except OSError:  # pragma: no cover - a file that vanished between listing and stat
        return ""


def _entry(path: Path) -> dict[str, Any]:
    is_dir = path.is_dir()
    return {
        "name": path.name,
        "kind": "folder" if is_dir else "file",
        "bytes": None if is_dir else path.stat().st_size,
        "modified": _stamp(path),
    }


async def list_dir(target: str, roots: list[str], *, limit: int) -> dict[str, Any]:
    """The entries of one folder, folders first, capped at `limit`."""
    path = resolve_inside(target, roots, must_exist=True)

    def work() -> dict[str, Any]:
        if not path.is_dir():
            return {"ok": False, "error": f"{path} is a file, not a folder"}
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return {"ok": False, "error": f"cannot read {path}: {exc.strerror or exc}"}
        entries = [_entry(child) for child in children[:limit]]
        return {
            "ok": True,
            "path": str(path),
            "entries": entries,
            # Said plainly, so the model knows it is looking at a part and can narrow instead of
            # concluding the folder has exactly this much in it.
            "truncated": len(children) > limit,
            "total": len(children),
        }

    return await asyncio.to_thread(work)


async def read_text(target: str, roots: list[str], *, max_bytes: int) -> dict[str, Any]:
    """The text of one file, or a refusal that says why."""
    path = resolve_inside(target, roots, must_exist=True)

    def work() -> dict[str, Any]:
        if path.is_dir():
            return {"ok": False, "error": f"{path} is a folder; use file.list"}
        size = path.stat().st_size
        try:
            head = path.read_bytes()[:max_bytes]
        except OSError as exc:
            return {"ok": False, "error": f"cannot read {path}: {exc.strerror or exc}"}
        if b"\x00" in head[:SNIFF_BYTES]:
            return {"ok": False, "error": f"{path.name} is not a text file"}
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            # Windows editors still write cp1252; try it before giving up, and say which was used.
            try:
                text = head.decode("cp1252")
            except UnicodeDecodeError:
                return {"ok": False, "error": f"{path.name} is not readable as text"}
            return {
                "ok": True,
                "path": str(path),
                "text": text,
                "encoding": "cp1252",
                "truncated": size > max_bytes,
                "bytes": size,
            }
        return {
            "ok": True,
            "path": str(path),
            "text": text,
            "encoding": "utf-8",
            "truncated": size > max_bytes,
            "bytes": size,
        }

    return await asyncio.to_thread(work)


async def write_text(
    target: str,
    text: str,
    roots: list[str],
    *,
    max_bytes: int,
    append: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write or append text to one file inside a root."""
    path = resolve_inside(target, roots)
    payload = text.encode("utf-8")
    if len(payload) > max_bytes:
        return {"ok": False, "error": f"that is {len(payload)} bytes; the limit is {max_bytes}"}

    def work() -> dict[str, Any]:
        if path.is_dir():
            return {"ok": False, "error": f"{path} is a folder"}
        if not path.parent.is_dir():
            # Creating it would build a tree from a typo, somewhere nobody looks.
            return {"ok": False, "error": f"the folder {path.parent} does not exist"}
        if path.exists() and not (append or overwrite):
            return {
                "ok": False,
                "error": f"{path.name} already exists; say so explicitly to replace it",
            }
        try:
            with path.open("a" if append else "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            return {"ok": False, "error": f"cannot write {path}: {exc.strerror or exc}"}
        log.info("files.written", path=str(path), bytes=len(payload), append=append)
        return {"ok": True, "path": str(path), "bytes": len(payload), "appended": append}

    return await asyncio.to_thread(work)


async def move(
    source: str, destination: str, roots: list[str], *, overwrite: bool = False
) -> dict[str, Any]:
    """Move or rename, with both ends inside a configured root."""
    from_path = resolve_inside(source, roots, must_exist=True)
    to_path = resolve_inside(destination, roots)

    def work() -> dict[str, Any]:
        target = to_path / from_path.name if to_path.is_dir() else to_path
        if not target.parent.is_dir():
            return {"ok": False, "error": f"the folder {target.parent} does not exist"}
        if target.exists() and not overwrite:
            return {"ok": False, "error": f"{target} already exists"}
        try:
            from_path.replace(target)
        except OSError as exc:
            return {"ok": False, "error": f"cannot move {from_path}: {exc.strerror or exc}"}
        log.info("files.moved", source=str(from_path), target=str(target))
        return {"ok": True, "from": str(from_path), "to": str(target)}

    return await asyncio.to_thread(work)


async def delete(target: str, roots: list[str]) -> dict[str, Any]:
    """Move one file or folder to the Recycle Bin. Never an unlink."""
    path = resolve_inside(target, roots, must_exist=True)

    def work() -> dict[str, Any]:
        try:
            to_recycle_bin(path)
        except RecycleError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(path), "recoverable": True}

    return await asyncio.to_thread(work)
