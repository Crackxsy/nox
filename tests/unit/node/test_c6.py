"""C6: the node's eyes, browser, desktop, computer use and media - routed through the same gate,
receipts, device confirmation, budget and audit as every node tool.

Nothing here touches the real screen, camera, input or sound: every platform layer is a fake
(grabbers, notices, the window probe, the input sender, the media runner)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.desktop.computer_use import DryRunSender
from nox.desktop.win32 import WindowInfo
from nox.lan.tls import TlsMaterial
from nox.node.gate import INPUT_TOOLS, NodeGate
from nox.node.listener import NodeListener
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.policy import relevant_groups
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier
from nox.node.registry import BLOB_RESULT_KEY, ROUTABLE, NodeRegistry
from nox.node.tools import NodeRefusal
from nox.node.world import GameLock, MediaArgs, PlatformMedia, World, world_tools
from nox.node.world_routes import NodeControl, capture_refusal, world_specs
from nox.security.model import PermissionRequest, Risk
from nox.security.permissions import grant_matches
from tests.unit.node import test_c4
from tests.unit.node.test_c4 import Confirmer, call, session_of, signed, start_node
from tests.unit.node.test_router import FakeRegistry, _entry

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100
LAPTOP = "a" * 32
BROWSER = "f" * 32
C6_GROUPS = ("screen", "camera", "browser", "desktop", "computer", "media")


class Probe:
    def __init__(self) -> None:
        self.focused: list[int] = []

    def windows(self) -> list[WindowInfo]:
        return [WindowInfo(7, "Notizen - Editor", 4242, "notepad.exe", False)]

    def focus(self, handle: int) -> bool:
        self.focused.append(handle)
        return True

    minimize = restore = close = focus


def _world(
    *,
    windows: bool = True,
    game: str = "",
    front: str = "",
    notice_fails: bool = False,
    computer_on: bool = True,
) -> tuple[World, dict[str, Any]]:
    seen: dict[str, Any] = {"notices": [], "sender": DryRunSender(), "grabs": 0}

    async def notice(text: str) -> None:
        if notice_fails:
            raise RuntimeError("no notification service")
        seen["notices"].append(text)

    async def grab() -> bytes:
        seen["grabs"] += 1
        return PNG

    lock = GameLock(
        watched=["RocketLeague.exe"],
        running_names=lambda: {game.lower()} if game else set(),
        foreground_name=lambda: front,
        process_pids=lambda _names: {999} if game else set(),
    )
    world = World(
        notice=notice,
        computer_enabled=lambda: computer_on,
        dialog_open=lambda: False,
        browser_dir=Path("unused-browser-dir"),
        lock=lock,
        screen=grab,
        camera=grab,
        windows=windows,
        probe=Probe,
        sender=lambda: seen["sender"],
    )
    return world, seen


def _gate(world: World, keys: Path) -> tuple[NodeGate, ReceiptSigner]:
    signer = ReceiptSigner(keys / "receipts")
    manifest = NodeManifest(groups={g: g in C6_GROUPS or g == "system" for g in GROUPS})
    gate = NodeGate(world_tools(world), manifest, None)
    gate.input_lock = world.lock.reason
    gate.receipts = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    return gate, signer


def _meta(signer: ReceiptSigner, name: str, args: dict[str, Any], risk: str) -> NoxCallMeta:
    meta = signed(signer, name, args, risk=risk, device_id="d" * 32, core_id="c" * 64)
    return NoxCallMeta.model_validate(meta)


# ---- the catalogue and its groups ---------------------------------------------------------------


def test_every_c6_tool_has_a_group_and_is_off_by_default_except_music() -> None:
    world, _ = _world()
    tools = world_tools(world)
    assert set(tools) <= ROUTABLE
    assert {t.group for t in tools.values()} == set(C6_GROUPS)
    defaults = NodeManifest()
    for group in C6_GROUPS:
        # Spec C §1.4: music is on by default (low risk); everything else only on the device.
        assert defaults.groups[group] is (group == "media"), group
    gate = NodeGate(tools, defaults, None)
    assert {t.name for t in gate.offered()} == {"media.control"}
    # Captures are high on a node: asked on the device, every time.
    assert tools["screen.capture"].risk == tools["camera.snapshot"].risk == "high"
    assert tools["computer.session_start"].risk == "high"
    assert "browser.screenshot" not in tools  # it would keep a file on the device


def test_the_dashboard_offers_the_new_groups() -> None:
    assert set(C6_GROUPS) <= relevant_groups()


def test_narrowing_on_the_core_hides_them(tmp_path: Path) -> None:
    entry = _entry(LAPTOP, "Laptop", tools={"screen.capture": "high", "media.control": "low"})
    entry.narrowed = frozenset({"screen"})
    assert not entry.offers("screen.capture") and entry.offers("media.control")
    world, _ = _world()
    gate, _signer = _gate(world, tmp_path)
    gate.narrow(["computer", "camera"])
    offered = {t.name for t in gate.offered()}
    assert not any(n.startswith(("computer.", "camera.")) for n in offered)
    assert "screen.capture" in offered


# ---- the game lock -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(INPUT_TOOLS))
@pytest.mark.parametrize(("game", "front", "reason"), [
    ("RocketLeague.exe", "", "game_running"),
    ("EasyAntiCheat.exe", "", "game_running"),
    ("", "rocketleague.exe", "game_in_foreground"),
])  # fmt: skip
def test_input_is_refused_on_the_node_while_a_game_runs(
    name: str, game: str, front: str, reason: str, tmp_path: Path
) -> None:
    """Even a core that signs everything gets no input through while a game runs here."""
    world, seen = _world(game=game, front=front)
    gate, signer = _gate(world, tmp_path)
    args = {"title": "Editor", "text": "hallo"} if name == "desktop.type_text" else {}
    decision = gate.check(name, args, meta=_meta(signer, name, args, "high"))
    assert not decision.allowed and decision.reason == reason
    assert seen["sender"].events == []


def test_the_game_lock_fails_closed_and_ending_a_session_is_never_locked(tmp_path: Path) -> None:
    world, _ = _world(game="RocketLeague.exe")
    gate, signer = _gate(world, tmp_path)
    meta = _meta(signer, "computer.session_end", {}, "low")
    assert gate.check("computer.session_end", {}, meta=meta).allowed

    def broken_check() -> str | None:
        raise RuntimeError("the process list cannot be read")

    gate.input_lock = broken_check
    broken = gate.check("computer.keys", {"combo": "ctrl+s"})
    assert broken.reason == "game_lock_unavailable"
    unset = NodeGate(world_tools(world), NodeManifest(), None)  # no lock installed at all
    assert unset.check("computer.keys", {"combo": "ctrl+s"}).reason == "game_lock_unavailable"
    assert gate.check("game.input.send", {}).reason == "hard_prohibition"


async def test_a_game_starting_inside_a_session_stops_it_before_the_next_event() -> None:
    names: set[str] = set()
    world, seen = _world()
    world.lock.running_names = lambda: names
    tools = world_tools(world)
    started = await tools["computer.session_start"].handler(
        tools["computer.session_start"].args.model_validate({"purpose": "Datei speichern"})
    )
    assert started["ok"] and started["session_active"]
    keys = tools["computer.keys"]
    assert (await keys.handler(keys.args.model_validate({"combo": "ctrl+s"})))["ok"]
    sent = len(seen["sender"].events)
    names.add("rocketleague.exe")  # the guard inside ComputerUse, before every single event
    refused = await keys.handler(keys.args.model_validate({"combo": "ctrl+s"}))
    assert refused == {"ok": False, "error": refused["error"], "session_active": False}
    assert len(seen["sender"].events) == sent


async def test_a_stop_ends_the_session_on_the_node() -> None:
    world, seen = _world()
    tools = world_tools(world)
    start = tools["computer.session_start"]
    await start.handler(start.args.model_validate({"purpose": "Fenster ordnen"}))
    world.stop("stopped_by_core")  # what `NodeAgent.on_stop` calls on nox/kill
    pointer = tools["computer.pointer"]
    answer = await pointer.handler(pointer.args.model_validate({"action": "click"}))
    assert answer["ok"] is False and answer["session_active"] is False
    assert seen["sender"].events == []
    await asyncio.sleep(0)  # the notices are shown in the background
    assert any("Maus und Tastatur" in n for n in seen["notices"])


async def test_desktop_and_computer_are_windows_only() -> None:
    world, seen = _world(windows=False)
    tools = world_tools(world)
    calls = {
        "computer.session_start": {"purpose": "Fenster ordnen"},
        "desktop.windows": {},
        "desktop.type_text": {"title": "x", "text": "y"},
    }
    for name, raw in calls.items():
        tool = tools[name]
        args = tool.args.model_validate(raw)
        with pytest.raises(NodeRefusal) as refused:
            await tool.handler(args)
        assert refused.value.reason == "windows_only"
    assert seen["sender"].events == []


def test_enabling_computer_needs_windows_and_a_second_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import cli
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "node")
    monkeypatch.setattr(cli, "computer_supported", lambda: False)
    with pytest.raises(cli.NodeCliError, match="Windows only"):
        cli.allow(
            home, "computer", True, say=print, interactive=lambda: True, confirm=lambda _p: True
        )
    monkeypatch.setattr(cli, "computer_supported", lambda: True)
    asked: list[str] = []

    def first_only(prompt: str) -> bool:
        asked.append(prompt)
        return len(asked) == 1

    with pytest.raises(cli.NodeCliError, match="Not allowed"):
        cli.allow(home, "computer", True, say=print, interactive=lambda: True, confirm=first_only)
    assert len(asked) == 2 and "mouse" in asked[1]


# ---- captures ------------------------------------------------------------------------------------


async def test_a_capture_shows_a_notice_first_and_keeps_nothing() -> None:
    world, seen = _world()
    tools = world_tools(world)
    result = await tools["camera.snapshot"].handler(tools["camera.snapshot"].args())
    assert seen["notices"] == ["Nox nimmt gerade ein Kamerabild auf (vom Core angefragt)."]
    assert result["_blob"] == PNG and result["sha256"] == hashlib.sha256(PNG).hexdigest()


async def test_no_visible_notice_no_picture() -> None:
    world, seen = _world(notice_fails=True)
    tools = world_tools(world)
    for name in ("screen.capture", "camera.snapshot"):
        with pytest.raises(NodeRefusal) as refused:
            await tools[name].handler(tools[name].args())
        assert refused.value.reason == "no_visible_indicator"
    assert seen["grabs"] == 0


async def test_a_capture_travels_as_blob_chunks_and_needs_the_device_dialog(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world, seen = _world()
    big = PNG + b"x" * (600 * 1024)  # more than one chunk
    world.screen = lambda: _value(big)
    monkeypatch.setattr(test_c4, "_tools", lambda _ran: world_tools(world))
    confirmer = Confirmer("allow")
    manifest = NodeManifest(groups={g: g in ("system", "screen") for g in GROUPS})
    node = await start_node(
        listener, pairing, tls, tmp_path, confirmer=confirmer, manifest=manifest
    )
    try:
        session = await session_of(listener, node.node_id)
        answer = await call(
            session, listener.signer, "screen.capture", {}, risk="high", call_id="cap1"
        )  # type: ignore[attr-defined]
        assert confirmer.asked and "screen.capture" in confirmer.asked[0]  # asked on the device
        assert "_blob" not in answer["structuredContent"]
        assert answer["structuredContent"]["sha256"] == hashlib.sha256(big).hexdigest()
        assert session.take_blob("cap1") == big
        confirmer.answer = "deny"
        denied = await call(session, listener.signer, "screen.capture", {}, risk="high")  # type: ignore[attr-defined]
        assert denied["_nox"]["refused"] == "denied_on_device"
        assert len(seen["notices"]) == 1  # no notice, no picture for the refused one
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def _value(data: bytes) -> bytes:
    return data


def _privacy(mode: str = "balanced", camera: bool = False, screen: bool = True) -> Any:
    from nox.core.state import PrivacyMode

    return SimpleNamespace(
        mode=PrivacyMode(mode),
        active_zone=None,
        allows_capture=lambda kind: {"camera": camera, "screen": screen}.get(kind, False),
        allows_cloud=lambda: mode in ("full", "balanced"),
        allows_screenshot_to_cloud=lambda: mode == "full",
    )


def test_this_cores_privacy_decides_before_a_node_is_asked() -> None:
    assert capture_refusal("screen", _privacy()) is None
    assert "camera capture is off" in str(capture_refusal("camera", _privacy()))
    assert "private" in str(capture_refusal("screen", _privacy("private")))
    assert capture_refusal("screen", None) is not None


def _specs(
    tmp_path: Path, privacy: Any, answer: Callable[..., Awaitable[dict[str, Any]]]
) -> tuple[dict[str, Any], FakeRegistry, NodeControl, list[Any]]:
    laptop = _entry(
        LAPTOP,
        "Laptop",
        tools={n: "high" for n in ROUTABLE if n.startswith(("screen", "camera", "computer"))},
        linked=BROWSER,
    )
    laptop.confirmer = True
    registry = FakeRegistry([laptop])
    registry.call = answer  # type: ignore[method-assign]
    granted: list[Any] = []
    engine = SimpleNamespace(grant_temporary=granted.append, revoke=lambda gid: granted.append(gid))
    notices: list[dict[str, Any]] = []
    control = NodeControl(engine, notices.append, linked=lambda _d: BROWSER)
    specs = world_specs(
        registry=registry,  # type: ignore[arg-type]
        own=lambda _n: None,
        defaults=dict,
        control=control,
        captures_root=lambda: tmp_path / "captures",
        privacy=lambda: privacy,
    )
    return {s.name: s for s in specs}, registry, control, [granted, notices]


async def test_a_node_capture_is_stored_on_this_core_and_the_model_gets_the_path(
    tmp_path: Path,
) -> None:
    sent: list[dict[str, Any]] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        sent.append(kw)
        meta = {"ok": True, "sha256": hashlib.sha256(PNG).hexdigest()}
        return {"device": entry.label, "result": meta, BLOB_RESULT_KEY: PNG}

    specs, _reg, _control, _ = _specs(tmp_path, _privacy(), answer)
    spec = specs["screen.capture"]
    payload = spec.input_model.model_validate({"device": "Laptop"}).model_dump()
    scope = spec.scope(payload)
    assert scope.risk is Risk.HIGH and scope.confirm_on_device and scope.rememberable is False
    result = await spec.handler(payload)
    assert result["ok"] and sent[0]["blob"] is True and BLOB_RESULT_KEY not in result
    path = Path(result["path"])
    assert path.parent == tmp_path / "captures" / "screen"
    assert await asyncio.to_thread(path.read_bytes) == PNG
    assert result["cloud_allowed"] is False  # BALANCED keeps screenshots local


async def test_a_capture_that_does_not_match_its_hash_or_privacy_is_refused(tmp_path: Path) -> None:
    calls: list[str] = []

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(tool)
        return {"device": entry.label, "result": {"ok": True, "sha256": "0" * 64}, "_blob": PNG}

    specs, _reg, _control, _ = _specs(tmp_path, _privacy(), answer)
    for name, error in (
        ("screen.capture", "malformed_result"),
        ("camera.snapshot", "capture.refused"),
    ):
        spec = specs[name]
        payload = spec.input_model.model_validate({"device": "Laptop"}).model_dump()
        spec.scope(payload)
        assert (await spec.handler(payload))["error"] == error
    assert calls == ["screen.capture"]  # the camera was never asked: off in this core's privacy
    assert not (tmp_path / "captures").exists() or not any((tmp_path / "captures").rglob("*.png"))


# ---- computer use on the core's side -------------------------------------------------------------


async def test_a_node_session_grants_input_on_that_node_only_and_shows_who_is_controlled(
    tmp_path: Path,
) -> None:
    active = {"value": True}

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            return {"device": entry.label, "result": {"ok": True, "minutes": 5}}
        return {"device": entry.label, "result": {"ok": True, "session_active": active["value"]}}

    specs, _reg, control, (granted, notices) = _specs(tmp_path, _privacy(), answer)
    start = specs["computer.session_start"]
    model = start.input_model.model_validate({"purpose": "Datei speichern", "device": "Laptop"})
    payload = model.model_dump()
    assert start.scope is not None and start.scope(payload).risk is Risk.HIGH
    await start.handler(payload)
    grant = granted[0]
    assert grant.scope == f"device:{LAPTOP}|session:computer/*" and grant.max_risk is Risk.MEDIUM

    def request(device: str) -> PermissionRequest:
        return PermissionRequest(
            agent="companion", tool="computer", action="pointer", mode="companion",
            risk=Risk.MEDIUM, device=device,
        )  # fmt: skip

    from datetime import UTC, datetime

    assert grant_matches(grant, request(LAPTOP), datetime.now(UTC))
    assert not grant_matches(grant, request("b" * 32), datetime.now(UTC))
    assert not grant_matches(grant, request(""), datetime.now(UTC))  # never this core
    assert notices[-1]["active"] and notices[-1]["device"].startswith("Laptop")
    # The browser on that PC may not answer "allow" while Nox controls it; another one may.
    assert control.blocks(BROWSER) and not control.blocks("e" * 32)
    # The node says the session is over: notice off, grant revoked, lock lifted.
    active["value"] = False
    keys = specs["computer.keys"]
    keys_payload = keys.input_model.model_validate({"combo": "ctrl+s", "device": "Laptop"})
    keys_args = keys_payload.model_dump()
    keys.scope(keys_args)
    await keys.handler(keys_args)
    assert notices[-1]["active"] is False and granted[-1] == grant.grant_id
    assert not control.blocks(BROWSER)


def test_the_kill_switch_ends_every_node_session() -> None:
    notices: list[dict[str, Any]] = []
    control = NodeControl(None, notices.append)
    control.start(_entry(LAPTOP, "Laptop"), 10)
    control.start(_entry("b" * 32, "Arbeits-PC"), 10)
    control.end_all("kill switch")
    assert control.sessions == {} and [n["active"] for n in notices[-2:]] == [False, False]


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
    world, _ = _world()
    played: list[str] = []

    class Media:
        async def control(self, action: str, level: int | None) -> dict[str, Any]:
            played.append(action)
            return {"ok": True, "action": action}

    world.media = Media()
    gate, signer = _gate(world, tmp_path)
    args = {"action": "quieter"}
    decision = gate.check("media.control", args, meta=_meta(signer, "media.control", args, "low"))
    assert decision.allowed and not decision.needs_local_confirmation
    assert gate.check("media.control", args).reason == "receipt_missing"
    assert decision.tool is not None and decision.args is not None
    await decision.tool.handler(decision.args)
    assert played == ["quieter"]


def test_the_routes_keep_registry_entries_consistent() -> None:
    accepted = NodeRegistry._accept_tools([{"name": n, "_nox": {"risk": "low"}} for n in ROUTABLE])
    assert set(accepted) == ROUTABLE
