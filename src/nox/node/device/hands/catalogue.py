"""`hand_tools`: the tools a device offers for files and the shell, each with its risk, its text
check and its handler."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nox.hands.shell import ProgramResolver, classify
from nox.node.device.hands.args import (
    MoveArgs,
    PathArgs,
    ShellArgs,
    WriteArgs,
    delete_refusal,
    move_refusal,
    shell_program,
    write_risk,
    write_summary,
    write_text_refusal,
)
from nox.node.device.hands.files import _list, _read, _write
from nox.node.device.hands.node_hands import NodeHands
from nox.node.device.safe_fs.core import OutsideFolders
from nox.node.device.safe_fs.folders import Folders
from nox.node.device.safe_fs.move_delete import move_file, trash_file, trash_support
from nox.node.device.tools import NodeRefusal, NodeTool


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
            path_safe=True,  # every open goes through nox.node.device.safe_fs
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
            path_safe=True,  # both ends go through nox.node.device.safe_fs
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
            path_safe=True,  # through nox.node.device.safe_fs, into the trash only
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
