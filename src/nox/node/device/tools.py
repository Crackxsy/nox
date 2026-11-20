"""The tools a node offers in this release: `system.info` (READ) and `notify.show` (LOW).

Tool names are the core's, so prompts, grants and the benchmark stay the same everywhere. Every
tool belongs to one capability group (`nox.node.wire.manifest.GROUPS`); its arguments are a strict
model, validated on the node before the handler runs - whatever the core sent.

`notify.show` passes title and text to the operating system's notification service as data
(environment variables / argv), never inside a command line or script text.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from nox.node.device.appid import toast_app_id
from nox.node.wire.arguments import NoArgs, NotifyArgs
from nox.node.wire.sysinfo import system_info

__all__ = [
    "NodeRefusal",
    "NodeTool",
    "Notifier",
    "PlatformNotifier",
    "default_tools",
]

_CREATE_NO_WINDOW = 0x08000000


Handler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


class NodeRefusal(Exception):  # noqa: N818 - a refusal, named for what it is
    """A tool refused on this device's own authority (outside the allowed folders, no sandbox):
    answered and audited like a refusal of `NodeGate`. `reason` is a short machine token."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        #: In a few words, what caused it (shown to the person, never a secret).
        self.detail = detail


@dataclass(frozen=True)
class NodeTool:
    name: str
    group: str
    risk: str
    description: str
    args: type[BaseModel]
    handler: Handler = field(compare=False)
    #: Declared by every tool with a side effect: it opens no path, it opens every path through
    #: `nox.node.device.safe_fs` (checked on the handle at open time), or it runs only inside the
    #: node's sandbox, which cannot see the node's folder (`nox.node.device.hands`). A tool without
    #: this declaration is not offered at all (`NodeGate`).
    path_safe: bool = False
    #: The risk of one call when it depends on its arguments (a write that replaces a file is
    #: higher than one that creates it); never below `risk`. None: always `risk`.
    risk_for: Callable[[BaseModel], str] | None = field(default=None, compare=False)
    #: What the confirmation on the device shows instead of the raw arguments (a write: the
    #: size and a preview, not a megabyte of text). None: every argument in full.
    shown: Callable[[BaseModel], dict[str, Any]] | None = field(default=None, compare=False)
    #: A check on the arguments' text alone, before the budget and before anyone is asked (a
    #: JSON file that does not parse is refused, not confirmed first). A reason, or None.
    precheck: Callable[[BaseModel], str | None] | None = field(default=None, compare=False)

    def call_risk(self, args: BaseModel) -> str:
        risk = self.risk_for(args) if self.risk_for is not None else self.risk
        order = ("read", "low", "medium", "high", "critical")
        if risk not in order:
            return "critical"
        return risk if order.index(risk) >= order.index(self.risk) else self.risk

    def describe(self) -> dict[str, Any]:
        """The MCP `tools/list` entry, with the Nox extensions the core routes by."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.args.model_json_schema(),
            "annotations": {"readOnlyHint": self.risk == "read"},
            "_nox": {"group": self.group, "risk": self.risk},
        }


# ---- system.info ---------------------------------------------------------------------------------


# ---- notify.show -------------------------------------------------------------------------------


class Notifier(Protocol):
    async def show(self, title: str, text: str) -> None: ...


#: The toast, built from environment variables inside PowerShell: the text never becomes code.
_WINDOWS_TOAST = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
    "ContentType=WindowsRuntime]|Out-Null;"
    "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(1);"
    "$n=$t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode($env:NOX_NOTIFY_TITLE))|Out-Null;"
    "$n.Item(1).AppendChild($t.CreateTextNode($env:NOX_NOTIFY_TEXT))|Out-Null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:NOX_NOTIFY_APPID)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)


def _powershell() -> Path:
    """Windows PowerShell by absolute path: never a `powershell.exe` that happens to be first on
    PATH or in the working folder."""
    system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
    return system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def notice_title(title: str) -> str:
    """A notice always says it comes from Nox - a core cannot make it look like the system's."""
    return "Nox" if title.strip() in ("", "Nox") else f"Nox: {title}"


class PlatformNotifier:
    """Windows toast, freedesktop `notify-send`, macOS Notification Center."""

    async def show(self, title: str, text: str) -> None:

        env = {
            **os.environ,
            "NOX_NOTIFY_TITLE": title,
            "NOX_NOTIFY_TEXT": text,
            "NOX_NOTIFY_APPID": toast_app_id(),  # the node's own ID once the installer set it
        }
        kwargs: dict[str, Any] = {"env": env, "stdout": subprocess.DEVNULL}
        if sys.platform == "win32":
            argv = [str(_powershell()), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST]
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        elif sys.platform == "darwin":
            script = (
                "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\n"
                "end run"
            )
            argv = ["osascript", "-e", script, title, text]
        else:
            if shutil.which("notify-send") is None:
                raise RuntimeError("no notification service (notify-send) on this device")
            argv = ["notify-send", "--app-name=Nox", "--", title, text]
        process = await asyncio.create_subprocess_exec(*argv, stderr=subprocess.PIPE, **kwargs)
        try:
            _out, err = await asyncio.wait_for(process.communicate(), 15)
        except TimeoutError:
            process.kill()
            raise RuntimeError("the notification service did not answer") from None
        if process.returncode != 0:
            raise RuntimeError(f"the notification failed: {err.decode(errors='replace')[:200]}")


def default_tools(notifier: Notifier | None = None) -> dict[str, NodeTool]:
    shown = notifier or PlatformNotifier()

    async def notify_show(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, NotifyArgs)
        await shown.show(notice_title(args.title), args.text)
        return {"shown": True}

    tools = [
        NodeTool(
            "system.info",
            "system",
            "read",
            "System values of this device: OS, CPU, memory, disk, battery, temperature, uptime.",
            NoArgs,
            system_info,
        ),
        NodeTool(
            "notify.show",
            "notify",
            "low",
            "Show a notification on this device.",
            NotifyArgs,
            notify_show,
            path_safe=True,  # opens no path
        ),
    ]
    return {tool.name: tool for tool in tools}
