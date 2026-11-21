"""`world_tools`: the tools a device offers for captures, the browser, the desktop, computer use
and media, each built on a `World`."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable
from typing import Any

from pydantic import BaseModel, ConfigDict

from nox.core.logging import get_logger
from nox.node.device.agent.calls import BLOB_KEY
from nox.node.device.tools import NodeRefusal, NodeTool
from nox.node.device.world.media import PlatformMedia
from nox.node.device.world.node_world import Grabber, World, _LazyProbe
from nox.node.device.world.notices import capture_notice, computer_notice
from nox.node.wire.arguments import MediaArgs, NoArgs
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


#: Browser tools offered on a node: not `browser.screenshot`, which keeps a file on the device.
NODE_BROWSER_TOOLS = (
    "browser.open",
    "browser.read",
    "browser.click",
    "browser.type",
    "browser.submit",
)


def _strict(model: type[BaseModel]) -> type[BaseModel]:
    """The same arguments, with nothing beyond them accepted (a node's rule for every tool)."""
    if model.model_config.get("extra") == "forbid":
        return model
    config = ConfigDict(**{**model.model_config, "extra": "forbid"})
    strict: type[BaseModel] = type(f"Node{model.__name__}", (model,), {"model_config": config})
    return strict


#: The most a capture may be on the wire (`nox/blob`, `protocol.MAX_RESULT_BYTES`).
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def _adapt(spec: ToolSpec, group: str, *, windows_only: World | None = None) -> NodeTool:
    model = _strict(spec.input_model)

    async def handler(args: BaseModel) -> dict[str, Any]:
        if windows_only is not None and not windows_only.windows:
            raise NodeRefusal("windows_only")
        result: dict[str, Any] = await spec.handler(args.model_dump())
        return result

    return NodeTool(
        spec.name,
        group,
        spec.risk.value,
        spec.description,
        model,
        handler,
        path_safe=True,  # opens no path the core names
    )


def _captures(world: World) -> list[NodeTool]:
    async def capture(kind: str, grab: Grabber | None) -> dict[str, Any]:
        try:
            await world.notice(capture_notice(kind))
        except Exception as exc:  # noqa: BLE001 - no visible notice, no picture
            log.warning("node.capture_notice_failed", kind=kind, error=type(exc).__name__)
            raise NodeRefusal("no_visible_indicator") from None
        if grab is None:
            from nox.hands.senses import grab_camera, grab_screen  # noqa: PLC0415

            grab = grab_screen if kind == "screen" else grab_camera
        from nox.hands.senses import CaptureUnavailable  # noqa: PLC0415

        try:
            png = await grab()
        except CaptureUnavailable as exc:
            return {"ok": False, "error": "capture.unavailable", "detail": str(exc)[:200]}
        if not png.startswith(PNG_MAGIC):
            return {"ok": False, "error": "capture.failed"}
        if len(png) > MAX_CAPTURE_BYTES:
            # A large screen: smaller first (PNG, longest side 2560 px), refused only if still big.
            smaller = await asyncio.to_thread(world.fit, png)
            if smaller is None or len(smaller) > MAX_CAPTURE_BYTES:
                return {"ok": False, "error": "capture.too_large", "bytes": len(png)}
            png = smaller
        return {
            "ok": True,
            "kind": kind,
            "media_type": "image/png",
            "bytes": len(png),
            "sha256": hashlib.sha256(png).hexdigest(),
            BLOB_KEY: png,
        }

    async def screen(_args: BaseModel) -> dict[str, Any]:
        return await capture("screen", world.screen)

    async def camera(_args: BaseModel) -> dict[str, Any]:
        return await capture("camera", world.camera)

    return [
        NodeTool(
            "screen.capture",
            "screen",
            "high",
            "A screenshot of this device's main screen, for the core that asked.",
            NoArgs,
            screen,
            path_safe=True,
        ),
        NodeTool(
            "camera.snapshot",
            "camera",
            "high",
            "One picture from this device's camera, for the core that asked.",
            NoArgs,
            camera,
            path_safe=True,
        ),
    ]


def _browser(world: World) -> list[NodeTool]:
    from nox.hands.browser import BrowserSession, BrowserSettings, register_browser_tools  # noqa: PLC0415
    from nox.sandbox.netguard import NetGuard  # noqa: PLC0415

    if world.browser is None:
        settings = BrowserSettings(
            enabled=True,
            profile_dir=str(world.browser_dir / "profile"),
            screenshots_dir=str(world.browser_dir / "unused"),
        )
        world.browser = BrowserSession(lambda: settings, NetGuard())
    scratch = ToolRegistry()
    register_browser_tools(scratch, world.browser)
    out = []
    for name in NODE_BROWSER_TOOLS:
        spec = scratch.get(name)
        assert spec is not None
        out.append(_adapt(spec, "browser"))
    return out


def _desktop_and_computer(world: World) -> list[NodeTool]:
    from nox.core.config.hands import ComputerUseConfig  # noqa: PLC0415
    from nox.desktop.computer_tools import register_computer_tools  # noqa: PLC0415
    from nox.desktop.computer_use import ComputerUse, terminal_runs_nox  # noqa: PLC0415
    from nox.desktop.process_probe import is_nox_process, nox_pids  # noqa: PLC0415
    from nox.desktop.tools import register_desktop_tools  # noqa: PLC0415

    settings = ComputerUseConfig()

    def notice(active: bool, _reason: str) -> None:
        # First: the report and the marker are right before anyone is told.
        world.session_changed(active, minutes=settings.session_minutes)
        world._spawn(_quiet(world.notice(computer_notice(active, settings.stop_hotkey))))

    computer = ComputerUse(
        enabled=world.computer_enabled,
        game_running=world.lock.running,
        game_in_foreground=world.lock.in_front,
        nox_pids=nox_pids,
        sender=world.sender,
        audit=world.audit,
        notice=notice,
        nox_process=is_nox_process,
        confirmation_pending=world.dialog_open,
        # A terminal running `nox-node` is Nox's own window here: never clicked.
        terminal_nox=lambda pid: terminal_runs_nox(pid, is_nox=is_nox_process),
    )
    scratch = ToolRegistry()
    # The permission side is the core's (a grant for this device); here the session is the
    # boundary: no action outside an open session, whatever the core sent.
    end = register_computer_tools(
        scratch, computer, lambda: settings, grant=lambda _minutes: "", revoke=lambda _g: None
    )
    world.end_session = end
    world.computer = computer
    register_desktop_tools(scratch, _LazyProbe(world.probe), world.lock.pids, input_guard=computer)
    out: list[NodeTool] = []
    for name in scratch.names():
        spec = scratch.get(name)
        assert spec is not None
        group = spec.name.split(".", 1)[0]
        tool = _adapt(spec, group, windows_only=world)
        out.append(tool)
    return out


async def _quiet(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - a notice that fails never stops anything
        log.warning("node.notice_failed", error=type(exc).__name__)


def withheld(world: World) -> dict[str, str]:
    """Tool families this device does not offer at all, and why (a missing library, the wrong
    OS). A grabber or a browser a test put in counts as there."""
    missing: dict[str, str] = {}
    if world.screen is None and not world.has_module("mss"):
        missing["screen.capture"] = "screen capture needs the extra `senses` (mss)"
    if world.camera is None and not world.has_module("cv2"):
        missing["camera.snapshot"] = "the camera needs the extra `senses` (OpenCV)"
    if world.browser is None and not world.has_module("playwright"):
        missing["browser."] = "the browser needs the extra `browser` (Playwright)"
    if not world.windows:
        missing["desktop."] = missing["computer."] = "Windows only"
    media = world.media
    if isinstance(media, PlatformMedia) and not media.platform.startswith(("win32", "linux")):
        missing["media.control"] = "no media backend on this OS"
    return missing


def world_tools(world: World) -> dict[str, NodeTool]:
    """The world tools of this device: captures, browser, desktop, computer use and media - those
    that can work here (`withheld`)."""

    async def media(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, MediaArgs)
        return await world.media.control(args.action, args.level)

    missing = withheld(world)
    tools = [
        *_captures(world),
        *([] if "browser." in missing else _browser(world)),
        *([] if "desktop." in missing else _desktop_and_computer(world)),
        NodeTool(
            "media.control",
            "media",
            "low",
            "Music and sound on this device: play, pause, next, previous, volume, mute.",
            MediaArgs,
            media,
            path_safe=True,  # opens no path
        ),
    ]
    return {
        tool.name: tool
        for tool in tools
        if tool.name not in missing and f"{tool.name.split('.', 1)[0]}." not in missing
    }
