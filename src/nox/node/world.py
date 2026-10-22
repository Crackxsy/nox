"""The node's eyes and the rest of its hands (C6): `screen.capture`, `camera.snapshot`,
`browser.*`, `desktop.*`, `computer.*` and `media.control` - the core's tool names, run on this
device under this device's rules (spec C §1.4).

Each is reached only through `NodeGate` (prohibitions, the game lock, stop state, the group this
device enabled - all off by default except `media` -, risk ceiling, the core's receipt, budget,
and the dialog on this device for high risk). On top of that:

* **Captures** (`screen.capture`, `camera.snapshot`) are high risk here: the person at this
  device says yes to every single one, in the dialog on this device. Before the picture is taken,
  a notification on this device says that Nox is taking it; when that notice cannot be shown, the
  capture is refused (`no_visible_indicator`) - never a silent picture. The picture is held in
  memory for this call only and goes to the core that asked as `nox/blob` chunks; nothing is
  written on this device.
* **`browser.*`** is Nox's own headless Chromium with a profile of its own under the node's
  folder, never the user's browser; every request goes through `NetGuard` (no LAN, no loopback),
  as on the core. `browser.screenshot` is not offered here (it would keep a file on the device).
* **`desktop.*`** and **`computer.*`** are Windows only; elsewhere every call is refused
  (`windows_only`). They are the core's own modules, unchanged: the boundary (`nox.desktop
  .boundary`: Windows itself, Nox, the game), the game guard inside `ComputerUse` before every
  single event, the self-approval lock (no input into Nox's windows or while a dialog is open), the
  stop hotkey. A session is opened only by `computer.session_start` (high risk: the dialog on this
  device, every session) and ends with a stop, a lost connection or the dead-man rule. No input
  synthesis happens in this module: it calls `nox.desktop.computer_use`, one of the two files the
  CI guard (Security Model §10) allows to synthesise input.
* **The game lock** (`GameLock`): `NodeGate` refuses mouse, keys and typing while a watched game
  or an anti-cheat service runs here or a game is in front, before anything else is looked at.
  `game.input.send` stays a hard prohibition.
* **`media.control`** (low): play, pause, next, previous, volume, mute - SMTC and Core Audio on
  Windows, MPRIS (`playerctl` or D-Bus) and PipeWire/PulseAudio on Linux. The action is one word
  of a fixed list and the level a number; neither ever becomes script text.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nox.core.logging import get_logger
from nox.node.agent import BLOB_KEY
from nox.node.tools import NodeRefusal, NodeTool
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = [
    "GameLock",
    "MediaArgs",
    "PlatformMedia",
    "World",
    "world_tools",
]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_CREATE_NO_WINDOW = 0x08000000
#: What the notice on this device says before a picture is taken.
CAPTURE_NOTICE = {
    "screen": "Nox nimmt gerade ein Bildschirmfoto auf (vom Core angefragt).",
    "camera": "Nox nimmt gerade ein Kamerabild auf (vom Core angefragt).",
}
COMPUTER_NOTICE_ON = "Nox steuert jetzt Maus und Tastatur auf diesem Gerät. {0} beendet sofort."
COMPUTER_NOTICE_OFF = "Nox steuert Maus und Tastatur nicht mehr."
#: Browser tools offered on a node: not `browser.screenshot`, which keeps a file on the device.
NODE_BROWSER_TOOLS = (
    "browser.open",
    "browser.read",
    "browser.click",
    "browser.type",
    "browser.submit",
)


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _strict(model: type[BaseModel]) -> type[BaseModel]:
    """The same arguments, with nothing beyond them accepted (a node's rule for every tool)."""
    if model.model_config.get("extra") == "forbid":
        return model
    config = ConfigDict(**{**model.model_config, "extra": "forbid"})
    strict: type[BaseModel] = type(f"Node{model.__name__}", (model,), {"model_config": config})
    return strict


# ---- the game lock -------------------------------------------------------------------------------


def _default_games() -> tuple[str, ...]:
    from nox.core.config.assistant import SensorGameConfig  # noqa: PLC0415

    return tuple(SensorGameConfig().process_names)


@dataclass
class GameLock:
    """Whether a game or an anti-cheat service keeps Nox's hands off this device right now.

    The watched games are the core's default list; anti-cheat services count whether or not a
    game is listed (`nox.desktop.install.ANTI_CHEAT_PROCESSES`). Read fresh on every call."""

    watched: Iterable[str] = field(default_factory=_default_games)
    running_names: Callable[[], set[str]] | None = None
    foreground_name: Callable[[], str] | None = None
    process_pids: Callable[[set[str]], set[int]] | None = None

    def _names(self) -> set[str]:
        from nox.desktop.install import ANTI_CHEAT_PROCESSES  # noqa: PLC0415

        return {n.lower() for n in self.watched} | set(ANTI_CHEAT_PROCESSES)

    def running(self) -> bool:
        from nox.desktop.install import _running_names  # noqa: PLC0415

        names = (self.running_names or _running_names)()
        return bool(names & self._names())

    def in_front(self) -> bool:
        if self.foreground_name is not None:
            name = self.foreground_name()
        else:
            from nox.desktop.computer_use import foreground_pid  # noqa: PLC0415
            from nox.desktop.install import _process_name  # noqa: PLC0415

            name = _process_name(foreground_pid())
        return name.lower() in self._names()

    def reason(self) -> str | None:
        """`NodeGate.input_lock`: why no input may be sent now, or None."""
        if self.running():
            return "game_running"
        if self.in_front():
            return "game_in_foreground"
        return None

    def pids(self) -> set[int]:
        """Process ids of the watched games and anti-cheat services (for `desktop.*`)."""
        names = self._names()
        if self.process_pids is not None:
            return self.process_pids(names)
        import psutil  # noqa: PLC0415

        found: set[int] = set()
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if str(process.info.get("name") or "").lower() in names:
                    found.add(int(process.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found


# ---- media.control -------------------------------------------------------------------------------

MediaAction = Literal[
    "play", "pause", "toggle", "next", "previous", "volume", "louder", "quieter", "mute", "unmute"
]
_TRANSPORT = ("play", "pause", "toggle", "next", "previous")


class MediaArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: MediaAction = Field(
        description="play, pause, toggle, next, previous, volume (with level), louder, quieter, "
        "mute or unmute."
    )
    level: int | None = Field(default=None, ge=0, le=100, description="Volume in percent.")

    @model_validator(mode="after")
    def _level_for_volume(self) -> MediaArgs:
        if (self.action == "volume") != (self.level is not None):
            raise ValueError("'level' belongs to the action 'volume', and only to it")
        return self


Runner = Callable[[list[str], dict[str, str]], Awaitable[tuple[int, str]]]


async def run_quietly(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run one program without a window, its output read, at most 15 s."""
    kwargs: dict[str, Any] = {"env": {**os.environ, **env}, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 15)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        raise
    return process.returncode or 0, out.decode("utf-8", "replace")[:200]


#: Windows: transport through the system media session (SMTC), volume through Core Audio. The
#: action and level come from the environment and are compared with fixed words; the C# source is
#: a constant.
_WINDOWS_MEDIA = (
    r"""$ErrorActionPreference='Stop'
$a=$env:NOX_MEDIA_ACTION
if($a -in 'volume','louder','quieter','mute','unmute'){
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume{int f();int g();int h();int i();
int SetMasterVolumeLevelScalar(float l,Guid c);int j();int GetMasterVolumeLevelScalar(out float l);
int k();int l();int m();int n();int SetMute([MarshalAs(UnmanagedType.Bool)]bool b,Guid c);
int GetMute(out bool b);}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice{int Activate(ref Guid id,int ctx,int p,out IAudioEndpointVolume v);}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator{int f();
int GetDefaultAudioEndpoint(int flow,int role,out IMMDevice d);}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject{}
public class NoxAudio{
static IAudioEndpointVolume V(){var e=new MMDeviceEnumeratorComObject() as IMMDeviceEnumerator;
IMMDevice d=null;Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(0,1,out d));
IAudioEndpointVolume v=null;var id=typeof(IAudioEndpointVolume).GUID;
Marshal.ThrowExceptionForHR(d.Activate(ref id,23,0,out v));return v;}
public static float Volume{get{float x;
Marshal.ThrowExceptionForHR(V().GetMasterVolumeLevelScalar(out x));return x;}
set{Marshal.ThrowExceptionForHR(V().SetMasterVolumeLevelScalar(value,Guid.Empty));}}
public static bool Mute{set{Marshal.ThrowExceptionForHR(V().SetMute(value,Guid.Empty));}}}
"@
switch($a){
'volume'{[NoxAudio]::Volume=[Math]::Min(100,[Math]::Max(0,[int]$env:NOX_MEDIA_LEVEL))/100}
'louder'{[NoxAudio]::Volume=[Math]::Min(1,[NoxAudio]::Volume+0.1)}
'quieter'{[NoxAudio]::Volume=[Math]::Max(0,[NoxAudio]::Volume-0.1)}
'mute'{[NoxAudio]::Mute=$true}
'unmute'{[NoxAudio]::Mute=$false}}
Write-Output ([Math]::Round([NoxAudio]::Volume*100));exit 0}
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$m=[System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object{
$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}|Select-Object -First 1
function W($op,$t){$k=$m.MakeGenericMethod($t).Invoke($null,@($op))
if(-not $k.Wait(5000)){exit 4};$k.Result}
"""
    r"$T=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,"
    r"Windows.Media.Control,ContentType=WindowsRuntime]"
    r"""
$s=(W ($T::RequestAsync()) $T).GetCurrentSession()
if(-not $s){exit 3}
switch($a){'play'{$o=$s.TryPlayAsync()}'pause'{$o=$s.TryPauseAsync()}'toggle'{$o=$s.TryTogglePlayPauseAsync()}
'next'{$o=$s.TrySkipNextAsync()}'previous'{$o=$s.TrySkipPreviousAsync()}default{exit 2}}
if(W $o ([bool])){exit 0}else{exit 5}
"""
)

_PLAYERCTL = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next"}
_MPRIS = {"play": "Play", "pause": "Pause", "toggle": "PlayPause", "next": "Next"}
_MPRIS_NAME = re.compile(r"'(org\.mpris\.MediaPlayer2\.[A-Za-z0-9_.\-]{1,120})'")


class MediaBackend(Protocol):
    async def control(self, action: str, level: int | None) -> dict[str, Any]: ...


@dataclass
class PlatformMedia:
    """The operating system's media controls, through `runner` (a test passes a fake)."""

    runner: Runner = run_quietly
    platform: str = sys.platform
    which: Callable[[str], str | None] = shutil.which

    async def control(self, action: str, level: int | None) -> dict[str, Any]:
        if self.platform == "win32":
            return await self._windows(action, level)
        if self.platform.startswith("linux"):
            return await self._linux(action, level)
        raise NodeRefusal("media_unsupported_here")

    async def _windows(self, action: str, level: int | None) -> dict[str, Any]:
        from nox.node.tools import _powershell  # noqa: PLC0415

        argv = [str(_powershell()), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_MEDIA]
        env = {"NOX_MEDIA_ACTION": action, "NOX_MEDIA_LEVEL": str(level or 0)}
        code, out = await self.runner(argv, env)
        return _answer(action, code, out)

    async def _linux(self, action: str, level: int | None) -> dict[str, Any]:
        if action in _TRANSPORT:
            argv = await self._mpris(action)
            if argv is None:
                return {"ok": False, "error": "no_player", "action": action}
            code, out = await self.runner(argv, {})
            return _answer(action, code, out)
        wpctl, pactl = self.which("wpctl"), self.which("pactl")
        if wpctl:
            sink = "@DEFAULT_AUDIO_SINK@"
            steps = {
                "volume": ["set-volume", sink, f"{(level or 0) / 100:.2f}"],
                "louder": ["set-volume", "-l", "1.0", sink, "10%+"],
                "quieter": ["set-volume", sink, "10%-"],
                "mute": ["set-mute", sink, "1"],
                "unmute": ["set-mute", sink, "0"],
            }
            argv = [wpctl, *steps[action]]
        elif pactl:
            sink = "@DEFAULT_SINK@"
            steps = {
                "volume": ["set-sink-volume", sink, f"{level or 0}%"],
                "louder": ["set-sink-volume", sink, "+10%"],
                "quieter": ["set-sink-volume", sink, "-10%"],
                "mute": ["set-sink-mute", sink, "1"],
                "unmute": ["set-sink-mute", sink, "0"],
            }
            argv = [pactl, *steps[action]]
        else:
            return {"ok": False, "error": "no_mixer", "action": action}
        code, out = await self.runner(argv, {})
        return _answer(action, code, out)

    async def _mpris(self, action: str) -> list[str] | None:
        playerctl = self.which("playerctl")
        if playerctl:
            return [playerctl, _PLAYERCTL.get(action, "previous")]
        gdbus = self.which("gdbus")
        if not gdbus:
            return None
        code, out = await self.runner(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.freedesktop.DBus",
                "--object-path",
                "/org/freedesktop/DBus",
                "--method",
                "org.freedesktop.DBus.ListNames",
            ],
            {},
        )
        found = _MPRIS_NAME.search(out) if code == 0 else None
        if found is None:
            return None
        method = "org.mpris.MediaPlayer2.Player." + _MPRIS.get(action, "Previous")
        return [
            gdbus,
            "call",
            "--session",
            "--dest",
            found[1],
            "--object-path",
            "/org/mpris/MediaPlayer2",
            "--method",
            method,
        ]


def _answer(action: str, code: int, out: str) -> dict[str, Any]:
    if code == 0:
        answer: dict[str, Any] = {"ok": True, "action": action}
        digits = out.strip()
        if digits.isdigit():
            answer["volume"] = int(digits)
        return answer
    reason = {3: "no_player", 4: "no_answer", 5: "player_refused"}.get(code, "failed")
    return {"ok": False, "error": reason, "action": action}


# ---- the tools -----------------------------------------------------------------------------------

Grabber = Callable[[], Awaitable[bytes]]
#: Shows a notice on this device (raises when it cannot).
Notice = Callable[[str], Awaitable[None]]


class _LazyProbe:
    """The window probe, made on first use (constructing it off Windows raises)."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self._make, self._probe = make, None

    def __getattr__(self, name: str) -> Any:
        if self._probe is None:
            self._probe = self._make()
        return getattr(self._probe, name)


def _real_probe() -> Any:
    from nox.desktop.win32 import RealWindowProbe  # noqa: PLC0415

    return RealWindowProbe()


def _real_sender() -> Any:
    from nox.core.config.hands import ComputerUseConfig  # noqa: PLC0415
    from nox.desktop.computer_use import Win32Sender, stop_hotkey_vks  # noqa: PLC0415

    return Win32Sender(stop_hotkey_vks(ComputerUseConfig().stop_hotkey))


@dataclass
class World:
    """Everything the C6 tools need from this device; each part can be replaced in a test."""

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
    windows: bool = sys.platform == "win32"
    probe: Callable[[], Any] = _real_probe
    sender: Callable[[], Any] = _real_sender
    browser: Any = None
    #: Ends a computer-use session (set by `world_tools`; called on every stop).
    end_session: Callable[[str], None] = lambda _reason: None
    _background: set[asyncio.Task[None]] = field(default_factory=set)

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
            await world.notice(CAPTURE_NOTICE[kind])
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
    from nox.desktop.computer_use import ComputerUse  # noqa: PLC0415
    from nox.desktop.install import _is_nox_process, _nox_pids  # noqa: PLC0415
    from nox.desktop.tools import register_desktop_tools  # noqa: PLC0415

    settings = ComputerUseConfig()

    def notice(active: bool, _reason: str) -> None:
        text = COMPUTER_NOTICE_ON.format(settings.stop_hotkey) if active else COMPUTER_NOTICE_OFF
        world._spawn(_quiet(world.notice(text)))

    computer = ComputerUse(
        enabled=world.computer_enabled,
        game_running=world.lock.running,
        game_in_foreground=world.lock.in_front,
        nox_pids=_nox_pids,
        sender=world.sender,
        audit=world.audit,
        notice=notice,
        nox_process=_is_nox_process,
        confirmation_pending=world.dialog_open,
    )
    scratch = ToolRegistry()
    # The permission side is the core's (a grant for this device); here the session is the
    # boundary: no action outside an open session, whatever the core sent.
    end = register_computer_tools(
        scratch, computer, lambda: settings, grant=lambda _minutes: "", revoke=lambda _g: None
    )
    world.end_session = end
    register_desktop_tools(scratch, _LazyProbe(world.probe), world.lock.pids, input_guard=computer)
    out: list[NodeTool] = []
    for name in scratch.names():
        spec = scratch.get(name)
        assert spec is not None
        group = spec.name.split(".", 1)[0]
        tool = _adapt(spec, group, windows_only=world)
        if spec.name in ("computer.pointer", "computer.keys", "computer.session_start"):
            tool = _with_session_state(tool, computer)
        out.append(tool)
    return out


async def _quiet(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - a notice that fails never stops anything
        log.warning("node.notice_failed", error=type(exc).__name__)


def _with_session_state(tool: NodeTool, computer: Any) -> NodeTool:
    """Every answer says whether the session is still open, so the core can end its notice."""
    inner = tool.handler

    async def handler(args: BaseModel) -> dict[str, Any]:
        result = await inner(args)
        return {**result, "session_active": bool(computer.active)}

    return NodeTool(
        tool.name, tool.group, tool.risk, tool.description, tool.args, handler, path_safe=True
    )


def world_tools(world: World) -> dict[str, NodeTool]:
    """The C6 tools of this device: captures, browser, desktop, computer use and media."""

    async def media(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, MediaArgs)
        return await world.media.control(args.action, args.level)

    tools = [
        *_captures(world),
        *_browser(world),
        *_desktop_and_computer(world),
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
    return {tool.name: tool for tool in tools}
