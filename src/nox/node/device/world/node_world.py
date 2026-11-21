"""`World`: what the node's capture, browser, desktop and media tools reach, behind seams a test
replaces, and the shrinking of a capture before it travels."""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
import sys
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.node.device.world.game_lock import GameLock
from nox.node.device.world.media import MediaBackend, PlatformMedia
from nox.node.device.world.notices import write_session_marker


def installed(module: str) -> bool:
    """Whether a Python module this device's tools need is installed (looked up, not imported)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


Grabber = Callable[[], Awaitable[bytes]]


#: Shows a notice on this device (raises when it cannot).
Notice = Callable[[str], Awaitable[None]]


MAX_SIDE_PX = 2560


def downscale_png(png: bytes, longest: int = MAX_SIDE_PX) -> bytes | None:
    """The picture with its longest side at most `longest` px, as PNG again (OpenCV, the
    `senses` extra); None without OpenCV or for a picture it cannot read."""
    try:
        import cv2  # noqa: PLC0415
        import numpy  # noqa: PLC0415
    except ImportError:
        return None
    image = cv2.imdecode(numpy.frombuffer(png, numpy.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = min(1.0, longest / max(height, width, 1))
    if scale < 1.0:
        size = (max(1, int(width * scale)), max(1, int(height * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    return bytes(encoded.tobytes()) if ok else None


class _LazyProbe:
    """The window probe, made on first use (constructing it off Windows raises)."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self._make, self._probe = make, None

    def __getattr__(self, name: str) -> Any:
        # A bound method is taken at registration (`probe.focus`): forward it, make nothing yet.
        def forward(*args: Any, **kwargs: Any) -> Any:
            if self._probe is None:
                self._probe = self._make()
            return getattr(self._probe, name)(*args, **kwargs)

        return forward


def _real_probe() -> Any:
    from nox.desktop.win32 import RealWindowProbe  # noqa: PLC0415

    return RealWindowProbe()


def _real_sender() -> Any:
    from nox.core.config.hands import ComputerUseConfig  # noqa: PLC0415
    from nox.desktop.computer_use import Win32Sender, stop_hotkey_vks  # noqa: PLC0415

    return Win32Sender(stop_hotkey_vks(ComputerUseConfig().stop_hotkey))


@dataclass
class World:
    """Everything the world tools need from this device; each part can be replaced in a test."""

    notice: Notice
    #: Whether `computer` is enabled here and the node is not stopped (read before every event).
    computer_enabled: Callable[[], bool]
    #: Whether a confirmation dialog is open on this device right now.
    dialog_open: Callable[[], bool]
    #: Where Nox's own browser keeps its profile on this device.
    browser_dir: Path
    audit: Callable[[str, str, str], None] = lambda _action, _target, _result: None
    lock: GameLock = field(default_factory=GameLock)
    screen: Grabber | None = None
    camera: Grabber | None = None
    media: MediaBackend = field(default_factory=PlatformMedia)
    #: Makes a too-large capture smaller (None: it cannot).
    fit: Callable[[bytes], bytes | None] = field(default=lambda png: downscale_png(png))
    windows: bool = sys.platform == "win32"
    probe: Callable[[], Any] = _real_probe
    sender: Callable[[], Any] = _real_sender
    browser: Any = None
    #: Ends a computer-use session (set by `world_tools`; called on every stop).
    end_session: Callable[[str], None] = lambda _reason: None
    #: The session generation: a random epoch per run (40 random high bits) plus a counter that
    #: goes up by one with every session start and end. A new run is told apart by its hello,
    #: which resets the core's view of this device (`NodeControl.hello`).
    gen: int = field(default_factory=lambda: secrets.randbits(40) << 20)
    session_id: str = ""
    computer: Any = None
    #: Whether a module is installed here (`installed`; a test passes its own answer).
    has_module: Callable[[str], bool] = installed
    #: Games the core asked this device to watch as well (`nox/games`); only ever grows.
    extra_games: set[str] = field(default_factory=set)
    #: The session marker in the node's folder (`SESSION_MARKER`); None: none is kept.
    marker: Path | None = None
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    def session_report(self) -> dict[str, Any]:
        """This device's truth about computer use (`protocol.SessionReport`), read now."""
        active = bool(self.computer is not None and self.computer.active)
        return {"gen": self.gen, "active": active, "id": self.session_id if active else ""}

    def add_games(self, names: Iterable[str]) -> None:
        """`nox/games`: more games to watch. Never a removal - that is the device's own word."""
        for name in list(names)[:64]:
            if len(self.extra_games) < 256:
                self.extra_games.add(name.strip())

    def session_changed(self, active: bool, *, minutes: int = 120) -> None:
        """A session started or ended here (`ComputerUse` says so through its notice): a new
        generation, and the marker the terminal commands read."""
        self.gen += 1
        self.session_id = uuid.uuid4().hex if active else ""
        if self.marker is not None:
            write_session_marker(self.marker, active, minutes=minutes)

    def stop(self, reason: str) -> None:
        """`NodeAgent.on_stop`: end a computer-use session and close the browser."""
        self.end_session(reason)
        if self.browser is not None:
            self._spawn(self.browser.close())

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)
