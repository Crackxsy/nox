"""The node's hands: `file.list/read/write/move/delete` and `shell.run` - the core's tool names,
run on this device under this device's rules (spec C §1.4, C5).

They run only after `NodeGate` allowed the call (prohibitions, stop state, capability group,
risk ceiling, arguments, the core's receipt, budget, and the confirmation on this device for high
risk). On top of that, each one holds its own boundary at the moment it acts:

* **`file.*`** open every path through `nox.node.safe_fs`: only inside a folder this device
  allowed (`nox node allow files --folder <path>`, none by default, never a whole drive, the home
  folder or a system folder), decided on the real file at open time, never inside the node's own
  folder or this user's secret stores. A write never lands where it could make something run
  outside the sandbox (`.git`, startup/autostart folders, shell start files, scripts and
  programs), never on an executable file or one with a second hard link, never overwrites unless
  asked and never creates folders. Replacing or appending is high risk (asked on the device);
  a new file is medium. A write that has started completes - a stop ends everything after it.
* **`file.move`** and **`file.delete`** are high risk (asked on the device every time, never
  remembered) and follow the same rules for both ends of a move and for the file deleted
  (`safe_fs.move_file`, `safe_fs.trash_file`): files only, never a link, a move never replaces
  anything, never creates a folder and never crosses drives; a delete goes into this user's trash
  (Recycle Bin, Trash, freedesktop trash) or does not happen - there is no unlink.
* **`shell.run`** runs **only** in this device's own `make_sandbox()`: AppContainer on Windows,
  bwrap on Linux - with strong guarantees, the weaker mode is never accepted on a node. The
  node's folder is not mounted (Linux; the run is refused when it would lie under a read-only
  system bind) or not granted (Windows: owner-only ACL, no container grant) inside it. When there
  is no such sandbox (macOS, no bwrap, a weak backend), the call is refused with
  `sandbox_unavailable` - there is no unsandboxed fallback. A stop of the call (`nox/kill`, the
  dead-man rule) kills the sandboxed process tree. On macOS, which has no such sandbox at all,
  `shell.run` is not even offered (`hand_tools`), so no status claims it.
* **`package.*`** is not offered on nodes yet: a package manager needs rights the sandbox does
  not give, so it could not work there (owner decision D6: a narrow privileged helper later).

A refusal is raised as `NodeRefusal(reason)`; the agent answers it like a gate refusal and writes
it to the node's audit trail.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nox.core.logging import get_logger
from nox.hands.shell import ProgramResolver, classify
from nox.node.safe_fs import (
    MAX_TEXT_BYTES,
    Folders,
    OutsideFolders,
    content_refusal,
    extension_of,
    list_folder,
    markdown_finding,
    move_file,
    open_existing,
    open_new,
    secret_locations,
    structured,
    trash_file,
    trash_support,
    write_refusal,
)
from nox.node.tools import NodeRefusal, NodeTool
from nox.sandbox import EgressDecl, Limits, Sandbox, SandboxUnavailable

log = get_logger(__name__)

__all__ = [
    "MoveArgs",
    "NodeHands",
    "NodeRefusal",
    "hand_tools",
    "write_risk",
    "write_summary",
    "write_text_refusal",
]

#: Below the agent's 30 s ceiling for a call with a side effect, so the sandbox ends the run
#: itself and reports `timed_out` instead of the call being cut off.
RUN_TIMEOUT_S = 25.0
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
#: Entries a listing shows, and entries it looks at at most (then it says it stopped).
MAX_LIST = 200
MAX_SCAN = 5000
#: A write's content is shown in full up to twice this; beyond, its head and its tail are
#: shown with a marker saying how much is not shown (never a cut text that looks complete).
PREVIEW_CHARS = 200
_SNIFF = 8192


# ---- arguments (strict: nothing the core sends beyond these fields is accepted) ----------------


class PathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)
    text: str = Field(max_length=MAX_WRITE_BYTES)
    append: bool = False
    overwrite: bool = False


class MoveArgs(BaseModel):
    """The core's `file.move` arguments. A node never replaces a file and never creates a folder:
    `overwrite` and `create_folders` are accepted only as false (`move_refusal`)."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=400)
    to: str = Field(min_length=1, max_length=400)
    overwrite: bool = False
    create_folders: bool = False


def move_refusal(path: str, to: str, *, overwrite: bool, create_folders: bool) -> str | None:
    """What refuses a move before anyone is asked - on the core and on the node: a replace, a
    new folder, or a name a write would be refused for at either end."""
    if overwrite:
        return "overwrite_refused"
    if create_folders:
        return "folders_not_created"
    source = _last_name(path)
    refusal = write_refusal([source]) if source else "invalid_path"
    if refusal is not None:
        return refusal
    target = _last_name(to)
    if not target:
        return "invalid_path"
    if "." in target.lstrip("."):  # a file name; a name without one may be a folder to move into
        return write_refusal([target])
    return "hidden_refused" if target.startswith(".") else None


def delete_refusal(path: str) -> str | None:
    """What refuses a delete before anyone is asked: a name a write would be refused for."""
    name = _last_name(path)
    return write_refusal([name]) if name else "invalid_path"


def _last_name(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    return parts[-1] if parts else ""


class ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str | None = Field(default=None, max_length=4000)
    argv: list[str] | None = Field(default=None, max_length=64)
    egress: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("argv")
    @classmethod
    def _short(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(len(token) > 1000 for token in value):
            raise ValueError("an argument is too long")
        return value

    @model_validator(mode="after")
    def _one_of(self) -> ShellArgs:
        if bool(self.command) == bool(self.argv):
            raise ValueError("give exactly one of 'command' or 'argv'")
        if any(len(host) > 253 for host in self.egress):
            raise ValueError("a host name is too long")
        return self


def write_risk(path: str, append: bool, overwrite: bool) -> str:
    """A new plain-data file is medium; replacing or extending a file, or any write of a
    STRUCTURED format (JSON, YAML) is high (asked on the device, never
    remembered) - on the core's side and on the node's alike."""
    return "high" if append or overwrite or structured(path) else "medium"


def write_text_refusal(path: str, text: str, *, append: bool) -> str | None:
    """The checks a write's text gets before anyone is asked - in the node's gate and on the
    core - and again when it runs: JSON/YAML is written whole (an append would check only a
    piece), must parse plainly and stay under `MAX_STRUCTURED_BYTES`; Markdown carries no code a
    note app would run."""
    if append and structured(path):
        return "append_refused"
    return content_refusal(path, text)


def write_summary(path: str, text: str, *, append: bool, overwrite: bool) -> dict[str, Any]:
    """What a person decides on for a write - the same fields in the core's permission target
    and in the device's dialog: where, how, how much (bytes, lines, a SHA-256 to compare), and
    the content - in full when it is short, otherwise its first and last `PREVIEW_CHARS`
    characters with an explicit "content shortened - N characters not shown"."""
    data = text.encode("utf-8", errors="replace")
    shown: dict[str, Any] = {
        "path": path,
        "mode": "append" if append else "overwrite" if overwrite else "new file",
        "bytes": len(data),
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "sha256": hashlib.sha256(data).hexdigest()[:16],
    }
    if len(text) <= 2 * PREVIEW_CHARS:
        shown["content"] = text
    else:
        hidden = len(text) - 2 * PREVIEW_CHARS
        shown["content_start"] = text[:PREVIEW_CHARS]
        shown["content_end"] = text[-PREVIEW_CHARS:]
        shown["not_shown"] = f"content shortened - {hidden} characters not shown"
    return shown


# ---- the hands -----------------------------------------------------------------------------------


def shell_program() -> tuple[str, ...]:
    """The OS shell for a `command` line, by absolute path (never one found on PATH)."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return (ps, "-NoProfile", "-NonInteractive", "-Command")
    return ("/bin/sh", "-c")


@dataclass
class NodeHands:
    """What the hands need from the node: the allowed folders (from the confirmed manifest, read
    per call) and the sandbox (made once, lazily; None when this device has none)."""

    folders: Callable[[], list[str]]
    protected: tuple[Path, ...]
    sandbox: Callable[[], Sandbox | None]
    trusted_dirs: tuple[str, ...] = ()
    timeout_s: float = RUN_TIMEOUT_S
    _cached: list[Sandbox | None] = field(default_factory=list)

    def resolved(self) -> Folders:
        from nox.paths import nox_home  # noqa: PLC0415

        # Nox's own folder too: a core on the same device keeps its database and config there.
        return Folders.build(self.folders(), [*self.protected, nox_home(), *secret_locations()])

    def exposed(self) -> str | None:
        """Why this device's sandbox would show something it must not: the node's folder, Nox's
        folder or a secret store under one of the system folders bwrap binds read-only."""
        if not sys.platform.startswith("linux"):
            return None
        from nox.paths import nox_home  # noqa: PLC0415
        from nox.sandbox.linux import visible_system_paths  # noqa: PLC0415

        private = [*self.protected, nox_home(), *secret_locations()]
        for place in private:
            real = os.path.realpath(place)
            for shown in visible_system_paths():
                if real == shown or real.startswith(shown.rstrip("/") + "/"):
                    return f"{real} lies under {shown}, which the sandbox shows read-only"
        return None

    async def confined(self) -> Sandbox:
        """This device's sandbox with strong guarantees, or `NodeRefusal("sandbox_unavailable")`."""
        if not self._cached:
            self._cached.append(await asyncio.to_thread(self.sandbox))
        sandbox = self._cached[0]
        if sandbox is None:
            raise NodeRefusal("sandbox_unavailable")
        problem = self.exposed()
        if problem is not None:
            log.error("node.sandbox_exposes_private_folder", detail=problem)
            raise NodeRefusal("sandbox_unavailable")
        try:
            await asyncio.to_thread(sandbox.check_usable)
        except SandboxUnavailable as exc:
            raise NodeRefusal("sandbox_unavailable") from exc
        return sandbox

    async def run(
        self, argv: list[str], *, egress: tuple[str, ...], workload: str
    ) -> dict[str, Any]:
        sandbox = await self.confined()
        try:
            result = await sandbox.run(
                argv,
                egress=EgressDecl(hosts=egress),
                limits=Limits(timeout_s=self.timeout_s),
                workload=workload,
            )
        except SandboxUnavailable as exc:
            raise NodeRefusal("sandbox_unavailable") from exc
        return {
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }


def default_sandbox() -> Sandbox | None:
    """`make_sandbox()` with the strong-guarantee rule; None where this OS has none."""
    from nox.sandbox import make_sandbox  # noqa: PLC0415 - heavy, only when a hand runs

    try:
        return make_sandbox(accept_weak=False)
    except SandboxUnavailable:
        return None


# ---- file.* --------------------------------------------------------------------------------------


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


def hand_tools(
    hands: NodeHands,
    *,
    platform: str = sys.platform,
    trash: Callable[[], str | None] = trash_support,
) -> dict[str, NodeTool]:
    """The hands this device can really offer: `shell.run` not on macOS (no sandbox backend
    there, so no status ever claims it), `file.delete` only where this user's trash can be used
    (`safe_fs.trash_support`)."""

    async def on_files(
        work: Callable[[Folders, threading.Event], dict[str, Any]],
    ) -> dict[str, Any]:
        stop = threading.Event()

        def guarded() -> dict[str, Any]:
            try:
                return work(hands.resolved(), stop)
            except OutsideFolders as exc:
                raise NodeRefusal(exc.reason) from None

        try:
            return await asyncio.to_thread(guarded)
        except asyncio.CancelledError:
            stop.set()  # a listing in progress ends at its next entry
            raise
        except NodeRefusal:
            raise
        except OSError as exc:
            return {"ok": False, "error": exc.strerror or type(exc).__name__}

    async def file_list(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        return await on_files(lambda f, stop: _list(f, args.path, stop))

    async def file_read(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        return await on_files(lambda f, _stop: _read(f, args.path))

    async def file_write(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, WriteArgs)
        return await on_files(lambda f, _stop: _write(f, args))

    async def shell_run(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, ShellArgs)
        info = classify(
            command=args.command,
            argv=args.argv,
            egress=args.egress,
            resolver=ProgramResolver.with_extra(hands.trusted_dirs),
            readonly_allowed=False,
        )
        argv = [*shell_program(), info.argv[0]] if info.needs_shell else list(info.argv)
        return await hands.run(argv, egress=tuple(args.egress), workload="shell")

    async def file_move(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, MoveArgs)
        refusal = move_refusal(
            args.path, args.to, overwrite=args.overwrite, create_folders=args.create_folders
        )
        if refusal is not None:
            raise NodeRefusal(refusal)

        def work(folders: Folders, _stop: threading.Event) -> dict[str, Any]:
            try:
                moved = move_file(folders, args.path, args.to)
            except FileExistsError:
                return {"ok": False, "error": "the target exists; a move never replaces a file"}
            except FileNotFoundError:
                return {"ok": False, "error": "no such file or folder (folders are never created)"}
            return {"ok": True, "from": moved.source, "to": moved.target}

        return await on_files(work)

    async def file_delete(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        refusal = delete_refusal(args.path)
        if refusal is not None:
            raise NodeRefusal(refusal)

        def work(folders: Folders, _stop: threading.Event) -> dict[str, Any]:
            try:
                where = trash_file(folders, args.path)
            except FileNotFoundError:
                return {"ok": False, "error": "no such file"}
            return {"ok": True, "path": args.path, "trashed_to": where, "recoverable": True}

        return await on_files(work)

    def precheck_move(args: BaseModel) -> str | None:
        assert isinstance(args, MoveArgs)
        return move_refusal(
            args.path, args.to, overwrite=args.overwrite, create_folders=args.create_folders
        )

    def shown_move(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, MoveArgs)
        return {"move": args.path, "to": args.to, "replaces": "never"}

    def precheck_delete(args: BaseModel) -> str | None:
        assert isinstance(args, PathArgs)
        return delete_refusal(args.path)

    def shown_delete(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        return {"delete": args.path, "goes to": "the trash (it can be put back)"}

    def risk_of_write(args: BaseModel) -> str:
        assert isinstance(args, WriteArgs)
        return write_risk(args.path, args.append, args.overwrite)

    def precheck_write(args: BaseModel) -> str | None:
        assert isinstance(args, WriteArgs)
        return write_text_refusal(args.path, args.text, append=args.append)

    def shown_write(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, WriteArgs)
        return write_summary(args.path, args.text, append=args.append, overwrite=args.overwrite)

    tools = [
        NodeTool(
            "file.list",
            "files",
            "read",
            "What is in one allowed folder on this device.",
            PathArgs,
            file_list,
        ),
        NodeTool(
            "file.read",
            "files",
            "read",
            "The text of one file in an allowed folder.",
            PathArgs,
            file_read,
        ),
        NodeTool(
            "file.write",
            "files",
            "medium",
            "Write a new text file in an allowed folder; replacing or appending asks first.",
            WriteArgs,
            file_write,
            path_safe=True,  # every open goes through nox.node.safe_fs
            risk_for=risk_of_write,
            shown=shown_write,
            precheck=precheck_write,
        ),
        NodeTool(
            "file.move",
            "files",
            "high",
            "Move or rename a file in the allowed folders; never replaces, never crosses drives.",
            MoveArgs,
            file_move,
            path_safe=True,  # both ends go through nox.node.safe_fs
            shown=shown_move,
            precheck=precheck_move,
        ),
        NodeTool(
            "file.delete",
            "files",
            "high",
            "Move a file in an allowed folder to this device's trash (it can be put back).",
            PathArgs,
            file_delete,
            path_safe=True,  # through nox.node.safe_fs, into the trash only
            shown=shown_delete,
            precheck=precheck_delete,
        ),
        NodeTool(
            "shell.run",
            "shell",
            "high",
            "Run a command in this device's sandbox: argv or one shell line; egress = hosts.",
            ShellArgs,
            shell_run,
            path_safe=True,  # runs only in the sandbox, which cannot see the node's folder
        ),
    ]
    withheld: set[str] = set()
    if platform == "darwin":
        withheld.add("shell.run")  # no sandbox backend on macOS: never offered, never claimed
    if trash() is not None:
        withheld.add("file.delete")
    return {tool.name: tool for tool in tools if tool.name not in withheld}
