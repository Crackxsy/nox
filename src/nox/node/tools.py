"""The tools a node offers in this release: `system.info` (READ) and `notify.show` (LOW).

Tool names are the core's, so prompts, grants and the benchmark stay the same everywhere. Every
tool belongs to one capability group (`nox.node.manifest.GROUPS`); its arguments are a strict
model, validated on the node before the handler runs - whatever the core sent.

`notify.show` passes title and text to the operating system's notification service as data
(environment variables / argv), never inside a command line or script text.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psutil
from pydantic import BaseModel, ConfigDict, Field

from nox import __version__

__all__ = ["NodeTool", "Notifier", "PlatformNotifier", "default_tools"]

_CREATE_NO_WINDOW = 0x08000000


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Nox", min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=400)


Handler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class NodeTool:
    name: str
    group: str
    risk: str
    description: str
    args: type[BaseModel]
    handler: Handler = field(compare=False)

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


async def system_info(_args: BaseModel) -> dict[str, Any]:
    return await asyncio.to_thread(_system_info)


def _system_info() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path.home().anchor or "/"))
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
        "cpu_count": psutil.cpu_count() or 0,
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "memory_total_mb": memory.total // (1024 * 1024),
        "memory_used_percent": memory.percent,
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "nox_version": __version__,
    }
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is not None:
        info["battery_percent"] = round(battery.percent)
        info["battery_plugged"] = bool(battery.power_plugged)
    temperatures = getattr(psutil, "sensors_temperatures", None)
    if temperatures is not None:
        try:
            readings = [t.current for entries in temperatures().values() for t in entries]
        except (OSError, RuntimeError):
            readings = []
        if readings:
            info["temperature_c"] = round(max(readings), 1)
    return info


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
    "$a='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($a)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)


class PlatformNotifier:
    """Windows toast, freedesktop `notify-send`, macOS Notification Center."""

    async def show(self, title: str, text: str) -> None:
        env = {**os.environ, "NOX_NOTIFY_TITLE": title, "NOX_NOTIFY_TEXT": text}
        kwargs: dict[str, Any] = {"env": env, "stdout": subprocess.DEVNULL}
        if sys.platform == "win32":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_TOAST]
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
        await shown.show(args.title, args.text)
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
        ),
    ]
    return {tool.name: tool for tool in tools}
