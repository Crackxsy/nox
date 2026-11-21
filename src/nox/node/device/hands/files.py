"""The handlers of `file.list`, `file.read` and `file.write`: every path is opened through
`nox.node.device.safe_fs`, inside a folder this device allows."""

from __future__ import annotations

import os
import stat
import threading
from typing import Any

from nox.node.device.hands.args import MAX_WRITE_BYTES, WriteArgs, write_text_refusal
from nox.node.device.safe_fs.core import OutsideFolders
from nox.node.device.safe_fs.folders import Folders
from nox.node.device.safe_fs.formats import extension_of, write_refusal
from nox.node.device.safe_fs.opening import list_folder, open_existing, open_new
from nox.node.device.safe_fs.text_checks import MAX_TEXT_BYTES, content_refusal, markdown_finding
from nox.node.device.tools import NodeRefusal

MAX_READ_BYTES = 256 * 1024


#: Entries a listing shows, and entries it looks at at most (then it says it stopped).
MAX_LIST = 200


MAX_SCAN = 5000


_SNIFF = 8192


def _kind(entry: os.DirEntry[str]) -> str:
    """A link - symlink, junction or any other reparse point - is reported as one, never as the
    folder it leads to."""
    info = entry.stat(follow_symlinks=False)
    if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
        return "link"
    if getattr(info, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return "link"
    return "folder" if entry.is_dir(follow_symlinks=False) else "file"


def _list(folders: Folders, path: str, stop: threading.Event) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    total = 0
    capped = False
    with list_folder(folders, path) as found:
        for entry in found:
            if stop.is_set():
                raise NodeRefusal("stopped")
            if total >= MAX_SCAN:
                capped = True
                break
            total += 1
            if len(entries) >= MAX_LIST:
                continue
            kind = _kind(entry)
            size = entry.stat(follow_symlinks=False).st_size if kind == "file" else None
            entries.append({"name": entry.name, "kind": kind, "bytes": size})
    entries.sort(key=lambda e: (e["kind"] != "folder", str(e["name"]).lower()))
    return {
        "ok": True,
        "path": path,
        "entries": entries,
        "total": total,
        "truncated": total > MAX_LIST or capped,
        "stopped_counting": capped,
    }


def _decode(head: bytes, truncated: bool) -> tuple[str, str]:
    """UTF-8 when it is UTF-8 - a sequence cut by the size limit is dropped at the last complete
    character, never re-read as another encoding; cp1252 only for text that is not UTF-8."""
    try:
        return head.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(head) - 3 and exc.reason == "unexpected end of data":
            return head[: exc.start].decode("utf-8"), "utf-8"
    return head.decode("cp1252", errors="replace"), "cp1252"


def _read(folders: Folders, path: str) -> dict[str, Any]:
    fd = open_existing(folders, path)
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        os.close(fd)
        return {"ok": False, "error": "that is a folder; use file.list"}
    size = info.st_size
    with os.fdopen(fd, "rb") as handle:
        head = handle.read(MAX_READ_BYTES)
    if b"\x00" in head[:_SNIFF]:
        return {"ok": False, "error": "not a text file"}
    text, encoding = _decode(head, size > MAX_READ_BYTES)
    return {
        "ok": True,
        "path": path,
        "text": text,
        "encoding": encoding,
        "bytes": size,
        "truncated": size > MAX_READ_BYTES,
    }


def _write(folders: Folders, args: WriteArgs) -> dict[str, Any]:
    _root, parts = folders.split(args.path)
    named = write_refusal(parts)  # the name first: a refused name is refused as such
    if named is not None:
        raise OutsideFolders(named, "only data files are written (allowlist)")
    refusal = write_text_refusal(args.path, args.text, append=args.append)
    if refusal is not None:
        raise NodeRefusal(refusal)
    payload = args.text.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        return {"ok": False, "error": f"{len(payload)} bytes; the limit is {MAX_WRITE_BYTES}"}
    try:
        fd = open_new(folders, args.path)
    except FileExistsError:
        if not (args.append or args.overwrite):
            return {"ok": False, "error": "the file exists; say so explicitly to replace it"}
        fd = open_existing(folders, args.path, write=True)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            return {"ok": False, "error": "that is a folder"}
        if args.append:
            # The whole file decides, read through this same handle: an append cannot assemble
            # in pieces what one write would be refused for.
            refused = _whole_file_refusal(fd, args)
            if refused is not None:
                os.close(fd)
                raise refused from None
            os.lseek(fd, 0, os.SEEK_END)
        else:
            os.ftruncate(fd, 0)  # only now: the handle was checked first
    except FileNotFoundError:
        return {"ok": False, "error": "the folder does not exist (folders are never created)"}
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return {"ok": True, "path": args.path, "bytes": len(payload), "appended": args.append}


def _whole_file_refusal(fd: int, args: WriteArgs) -> NodeRefusal | None:
    """For an append: the content check on what the file holds plus the new text, read through
    the already verified handle. A file too large to read whole is not appended to. When the
    file already holds what blocks it, the refusal says what that is."""
    size = os.fstat(fd).st_size
    if size > min(MAX_WRITE_BYTES, MAX_TEXT_BYTES):
        return NodeRefusal("too_large", "the file is already larger than a node writes")
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    existing = b"".join(chunks).decode("utf-8", errors="replace")
    refusal = content_refusal(args.path, existing + args.text)
    if refusal is None:
        return None
    if extension_of(args.path) == ".md":
        found = markdown_finding(existing) or markdown_finding(args.text)
        where = "already in the file" if markdown_finding(existing) else "in the new text"
        if found is not None:
            return NodeRefusal(refusal, f"{found} {where}")
    return NodeRefusal(refusal)
