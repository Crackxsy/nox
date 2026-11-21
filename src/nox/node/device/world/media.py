"""`media.control` on this device: the system's transport keys and the playing media session, on
Windows (SMTC, Core Audio) and Linux (MPRIS, PipeWire/PulseAudio); macOS has no backend."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from nox.node.device.tools import NodeRefusal, _powershell

_CREATE_NO_WINDOW = 0x08000000


_TRANSPORT = ("play", "pause", "toggle", "next", "previous")


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
