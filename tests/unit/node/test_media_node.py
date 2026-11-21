"""Media keys on a node: a fixed list of actions passed as data, MPRIS and the mixer on
Linux, and music that runs with a receipt, without a dialog, announced once."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.node.device.cli import pairing as cli_pairing
from nox.node.device.cli import runtime as cli_runtime
from nox.node.device.home import NodeHome
from nox.node.device.tools import NodeRefusal
from nox.node.device.world.media import PlatformMedia
from nox.node.wire.arguments import MediaArgs
from tests.unit.node.support_sessions import device_world, world_gate, world_meta

# ---- media ---------------------------------------------------------------------------------------


def test_media_arguments_are_a_fixed_list() -> None:
    assert MediaArgs.model_validate({"action": "volume", "level": 30}).level == 30
    for bad in ({"action": "volume"}, {"action": "next", "level": 3}, {"action": "rm -rf"}):
        with pytest.raises(ValueError):
            MediaArgs.model_validate(bad)


async def test_media_on_windows_passes_the_action_as_data() -> None:
    ran: list[tuple[list[str], dict[str, str]]] = []

    async def runner(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
        ran.append((argv, env))
        return 0, "40\n"

    media = PlatformMedia(runner=runner, platform="win32")
    assert await media.control("quieter", None) == {"ok": True, "action": "quieter", "volume": 40}
    argv, env = ran[0]
    assert argv[0].lower().endswith("powershell.exe") and env["NOX_MEDIA_ACTION"] == "quieter"
    assert "quieter" not in argv[-1].split("$a=")[0]  # the script is a constant


async def test_media_on_linux_uses_mpris_and_the_mixer() -> None:
    ran: list[list[str]] = []

    async def runner(argv: list[str], _env: dict[str, str]) -> tuple[int, str]:
        ran.append(argv)
        if argv[-1] == "org.freedesktop.DBus.ListNames":
            return 0, "(['org.freedesktop.DBus', 'org.mpris.MediaPlayer2.example'],)"
        return 0, ""

    tools = {"playerctl": "/usr/bin/playerctl", "wpctl": "/usr/bin/wpctl"}
    media = PlatformMedia(runner=runner, platform="linux", which=tools.get)
    assert (await media.control("toggle", None))["ok"]
    assert (await media.control("volume", 25))["ok"]
    assert ran[0] == ["/usr/bin/playerctl", "play-pause"]
    assert ran[1] == ["/usr/bin/wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.25"]
    gdbus = PlatformMedia(runner=runner, platform="linux", which={"gdbus": "/usr/bin/gdbus"}.get)
    assert (await gdbus.control("next", None))["ok"]
    assert ran[-1][4] == "org.mpris.MediaPlayer2.example" and ran[-1][-1].endswith(".Next")
    none = PlatformMedia(runner=runner, platform="linux", which=lambda _n: None)
    assert (await none.control("mute", None))["error"] == "no_mixer"
    with pytest.raises(NodeRefusal):
        await PlatformMedia(runner=runner, platform="darwin").control("play", None)


async def test_music_runs_with_a_receipt_and_without_a_dialog(tmp_path: Path) -> None:
    world, _ = device_world()
    played: list[str] = []

    class Media:
        async def control(self, action: str, level: int | None) -> dict[str, Any]:
            played.append(action)
            return {"ok": True, "action": action}

    world.media = Media()
    gate, signer = world_gate(world, tmp_path)
    args = {"action": "quieter"}
    decision = gate.check(
        "media.control", args, meta=world_meta(signer, "media.control", args, "low")
    )
    assert decision.allowed and not decision.needs_local_confirmation
    assert gate.check("media.control", args).reason == "receipt_missing"
    assert decision.tool is not None and decision.args is not None
    await decision.tool.handler(decision.args)
    assert played == ["quieter"]


async def test_music_is_announced_once_on_the_device(tmp_path: Path) -> None:
    shown: list[str] = []

    class Notifier:
        async def show(self, title: str, text: str) -> None:
            shown.append(text)

    home = NodeHome(tmp_path / "node")
    cli_pairing.confirm_defaults(home, None)
    runtime = cli_runtime.build(home, notifier=Notifier())
    assert await cli_runtime.media_notice_once(runtime) is True
    assert await cli_runtime.media_notice_once(runtime) is False
    assert shown == [cli_runtime.media_notice()]
