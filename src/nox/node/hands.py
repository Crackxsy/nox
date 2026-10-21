"""The node's hands: `file.list/read/write`, `shell.run` and `package.install/remove` - the core's
tool names, run on this device under this device's rules (spec C §1.4, C5).

They run only after `NodeGate` allowed the call (prohibitions, stop state, capability group,
risk ceiling, arguments, the core's receipt, budget, and the confirmation on this device for high
risk). On top of that, each one holds its own boundary at the moment it acts:

* **`file.*`** open every path through `nox.node.safe_fs`: only inside a folder this device
  allowed (`nox node allow files --folder <path>`, none by default), decided on the real file at
  open time, never inside the node's own folder or the keyring's folders. Writes never overwrite
  unless asked, never create folders, and are size-capped; reads refuse what is not text.
* **`shell.run` and `package.*`** run **only** in this device's own `make_sandbox()`: AppContainer
  on Windows, bwrap on Linux - with strong guarantees, the weaker mode is never accepted on a
  node. The node's folder is not mounted (Linux) or not granted (Windows: owner-only ACL, no
  container grant) inside it. When there is no such sandbox (macOS, no bwrap, a weak backend),
  the call is refused with `sandbox_unavailable` - there is no unsandboxed fallback. A stop of the
  call (`nox/kill`, the dead-man rule) kills the sandboxed process tree.

A refusal is raised as `NodeRefusal(reason)`; the agent answers it like a gate refusal and writes
it to the node's audit trail.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nox.hands.packages import _PACKAGE_ID, apt_argv, winget_argv
from nox.hands.shell import ProgramResolver, classify
from nox.node.safe_fs import (
    Folders,
    OutsideFolders,
    keyring_folders,
    list_folder,
    open_existing,
    open_new,
)
from nox.node.tools import NodeRefusal, NodeTool
from nox.sandbox import EgressDecl, Limits, Sandbox, SandboxUnavailable

__all__ = ["NodeHands", "NodeRefusal", "hand_tools"]

#: Below the agent's 30 s ceiling for a call with a side effect, so the sandbox ends the run
#: itself and reports `timed_out` instead of the call being cut off.
RUN_TIMEOUT_S = 25.0
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST = 200
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


class PackageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    package_id: str = Field(pattern=_PACKAGE_ID.pattern)


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
        return Folders.build(self.folders(), [*self.protected, *keyring_folders()])

    async def confined(self) -> Sandbox:
        """This device's sandbox with strong guarantees, or `NodeRefusal("sandbox_unavailable")`."""
        if not self._cached:
            self._cached.append(await asyncio.to_thread(self.sandbox))
        sandbox = self._cached[0]
        if sandbox is None:
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


def _refused(exc: OutsideFolders) -> NodeRefusal:
    return NodeRefusal(exc.reason)


def _list(folders: Folders, path: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    total = 0
    with list_folder(folders, path) as found:
        for entry in found:
            total += 1
            if len(entries) >= MAX_LIST:
                continue
            info = entry.stat(follow_symlinks=False)
            kind = (
                "link"
                if entry.is_symlink()
                else "folder"
                if entry.is_dir(follow_symlinks=False)
                else "file"
            )
            entries.append(
                {
                    "name": entry.name,
                    "kind": kind,
                    "bytes": None if kind != "file" else info.st_size,
                }
            )
    entries.sort(key=lambda e: (e["kind"] != "folder", str(e["name"]).lower()))
    return {
        "ok": True,
        "path": path,
        "entries": entries,
        "total": total,
        "truncated": total > MAX_LIST,
    }


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
    try:
        text, encoding = head.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = head.decode("cp1252", errors="replace"), "cp1252"
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
    def on_files(work: Callable[[Folders], dict[str, Any]]) -> Awaitable[dict[str, Any]]:
        async def run() -> dict[str, Any]:
            def guarded() -> dict[str, Any]:
                try:
                    return work(hands.resolved())
                except OutsideFolders as exc:
                    raise _refused(exc) from None

            try:
                return await asyncio.to_thread(guarded)
            except NodeRefusal:
                raise
            except OSError as exc:
                return {"ok": False, "error": exc.strerror or type(exc).__name__}

        return run()

    async def file_list(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        return await on_files(lambda f: _list(f, args.path))

    async def file_read(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, PathArgs)
        return await on_files(lambda f: _read(f, args.path))

    async def file_write(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, WriteArgs)
        return await on_files(lambda f: _write(f, args))

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

    def package(action: str) -> Any:
        async def handler(args: BaseModel) -> dict[str, Any]:
            assert isinstance(args, PackageArgs)
            make = winget_argv if sys.platform == "win32" else apt_argv
            # It downloads: any public host through the run's own proxy - never the LAN.
            return await hands.run(make(action, args.package_id), egress=("*",), workload="package")

        return handler

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
            "Write or append text to a file in an allowed folder (never overwrites unless asked).",
            WriteArgs,
            file_write,
            path_safe=True,  # every open goes through nox.node.safe_fs
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
        NodeTool(
            "package.install",
            "packages",
            "high",
            "Install a program by its package id (winget / apt), in this device's sandbox.",
            PackageArgs,
            package("install"),
            path_safe=True,
        ),
        NodeTool(
            "package.remove",
            "packages",
            "high",
            "Remove a program by its package id (winget / apt), in this device's sandbox.",
            PackageArgs,
            package("remove"),
            path_safe=True,
        ),
    ]
    return {tool.name: tool for tool in tools}
