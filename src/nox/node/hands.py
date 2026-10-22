"""The node's hands: `file.list/read/write` and `shell.run` - the core's tool names, run on this
device under this device's rules (spec C §1.4, C5).

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
* **`shell.run`** runs **only** in this device's own `make_sandbox()`: AppContainer on Windows,
  bwrap on Linux - with strong guarantees, the weaker mode is never accepted on a node. The
  node's folder is not mounted (Linux; the run is refused when it would lie under a read-only
  system bind) or not granted (Windows: owner-only ACL, no container grant) inside it. When there
  is no such sandbox (macOS, no bwrap, a weak backend), the call is refused with
  `sandbox_unavailable` - there is no unsandboxed fallback. A stop of the call (`nox/kill`, the
  dead-man rule) kills the sandboxed process tree.
* **`package.*`** is not offered on nodes yet: a package manager needs rights the sandbox does
  not give, so it could not work there.

A refusal is raised as `NodeRefusal(reason)`; the agent answers it like a gate refusal and writes
it to the node's audit trail.
"""

from __future__ import annotations

import asyncio
import os
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
    Folders,
    OutsideFolders,
    list_folder,
    open_existing,
    open_new,
    secret_locations,
)
from nox.node.tools import NodeRefusal, NodeTool
from nox.sandbox import EgressDecl, Limits, Sandbox, SandboxUnavailable

log = get_logger(__name__)

__all__ = ["NodeHands", "NodeRefusal", "hand_tools", "write_risk", "write_summary"]

#: Below the agent's 30 s ceiling for a call with a side effect, so the sandbox ends the run
#: itself and reports `timed_out` instead of the call being cut off.
RUN_TIMEOUT_S = 25.0
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
#: Entries a listing shows, and entries it looks at at most (then it says it stopped).
MAX_LIST = 200
MAX_SCAN = 5000
PREVIEW_CHARS = 80
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


def write_risk(append: bool, overwrite: bool) -> str:
    """A new file is medium; replacing or extending an existing one is high (asked on the
    device, never remembered) - on the core's side and on the node's alike."""
    return "high" if append or overwrite else "medium"


def write_summary(path: str, text: str, *, append: bool, overwrite: bool) -> dict[str, Any]:
    """What a person decides on for a write: where, how, how much, and how it starts - the same
    fields in the core's permission target and in the device's dialog."""
    mode = "append" if append else "overwrite" if overwrite else "new file"
    preview = text[:PREVIEW_CHARS] + (" [...]" if len(text) > PREVIEW_CHARS else "")
    return {
        "path": path,
        "mode": mode,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "preview": preview,
    }


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
        return Folders.build(self.folders(), [*self.protected, *secret_locations()])

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
            os.lseek(fd, 0, os.SEEK_END)
        else:
            os.ftruncate(fd, 0)  # only now: the handle was checked first
    except FileNotFoundError:
        return {"ok": False, "error": "the folder does not exist (folders are never created)"}
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return {"ok": True, "path": args.path, "bytes": len(payload), "appended": args.append}


def hand_tools(hands: NodeHands) -> dict[str, NodeTool]:
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

    def risk_of_write(args: BaseModel) -> str:
        assert isinstance(args, WriteArgs)
        return write_risk(args.append, args.overwrite)

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
    return {tool.name: tool for tool in tools}
