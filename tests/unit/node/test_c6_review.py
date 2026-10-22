"""C6 external review: a fail-closed game lock, approvals while Nox controls a node (PIN, remote
sessions, paired devices), the order of the gate's checks, cancelled starts, timeouts that wait
for the device's dialog, privacy rechecked after it, and the smaller points. Every platform
layer is a fake: no real screen, input, service query or remote-session check runs here."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.node import cli as node_cli
from nox.node.confirmed import effective_manifest
from nox.node.gate import INPUT_TOOLS, LOCKED_TOOLS
from nox.node.home import NodeHome
from nox.node.registry import BLOB_RESULT_KEY, NodeEntry, NodeRegistry
from nox.node.router import ROUTED_TIMEOUT_S
from nox.node.world import GameLock, world_tools
from nox.node.world_routes import (
    MAX_SESSION_MINUTES,
    REFUSE_DEVICE,
    REFUSE_NO_PIN,
    REFUSE_REMOTE,
    NodeControl,
    approval_refusal,
)
from nox.security.gate import PinRequiredError
from tests.unit.node.test_c6 import (
    BROWSER,
    LAPTOP,
    PNG,
    _answer_with,
    _gate,
    _privacy,
    _report,
    _route,
    _specs,
    _start,
    _world,
)

# ---- H1: the game lock ---------------------------------------------------------------------------


def _lock(**over: Any) -> GameLock:
    fields: dict[str, Any] = {
        "watched": lambda: ["RocketLeague.exe"],
        "running_names": set,
        "foreground_name": lambda: "explorer.exe",
        "process_pids": lambda _n: set(),
        "services": set,
        "screen_state": lambda: 5,
        "windows": True,
    }
    fields.update(over)
    return GameLock(**fields)


def _boom() -> Any:
    raise OSError("cannot be read")


def test_a_free_desktop_is_not_locked() -> None:
    assert _lock().reason() is None


@pytest.mark.parametrize("state", [2, 3, 4])  # busy, full-screen Direct3D, presentation mode
def test_anything_that_owns_the_screen_locks_whatever_its_name(state: int) -> None:
    assert _lock(screen_state=lambda: state).reason() == "game_in_foreground"


@pytest.mark.parametrize(
    "over",
    [
        {"screen_state": _boom},
        {"foreground_name": _boom},
        {"foreground_name": lambda: ""},  # no foreground window, or its process unreadable
        {"services": _boom},
        {"running_names": _boom},
    ],
)
def test_every_check_that_cannot_answer_locks(over: dict[str, Any]) -> None:
    assert _lock(**over).reason() in ("game_running", "game_in_foreground")


@pytest.mark.parametrize("service", ["vgk", "BEDaisy", "EasyAntiCheat_EOS", "mhyprot2"])
def test_anti_cheat_services_and_drivers_lock(service: str) -> None:
    assert _lock(services=lambda: {service}).reason() == "game_running"


@pytest.mark.parametrize("process", ["SGuard64.exe", "GameMon64.exe", "PnkBstrB.exe", "xxd-0.xem"])
def test_anti_cheat_processes_lock_by_name_and_family(process: str) -> None:
    assert _lock(running_names=lambda: {process.lower()}).reason() == "game_running"
    assert _lock(foreground_name=lambda: process).reason() == "game_in_foreground"


def test_the_service_check_asks_for_the_drivers_too() -> None:
    from nox.desktop.anticheat import ANTI_CHEAT_SERVICES

    for name in ("vgc", "vgk", "BEService", "BEDaisy", "FACEIT", "xhunter1", "atvi-randgrid_sr"):
        assert name in ANTI_CHEAT_SERVICES


def test_off_windows_there_is_no_input_at_all() -> None:
    assert _lock(windows=False).reason() == "windows_only"


def test_the_devices_own_games_lock_and_only_the_device_removes_them(tmp_path: Path) -> None:
    home = NodeHome(tmp_path / "node")
    node_cli.confirm_defaults(home, None)
    node_cli.games(home, "ExampleGame.exe", True, say=print)
    runtime = node_cli.build(home, notifier=SimpleNamespace())
    assert runtime.world is not None
    assert "ExampleGame.exe" in runtime.gate.manifest.games
    assert "examplegame.exe" in runtime.world.lock._names()
    # Editing node.yaml cannot drop a confirmed game: games only ever add up.
    from nox.node.manifest import load_manifest, save_manifest

    found = load_manifest(home.manifest_file)
    save_manifest(home.manifest_file, found.model_copy(update={"games": []}))
    audit = node_cli.open_audit(home)
    key = node_cli.manifest_key(home.path, None)
    assert (
        "ExampleGame.exe" in effective_manifest(load_manifest(home.manifest_file), audit, key).games
    )
    with pytest.raises(node_cli.NodeCliError, match="interactive"):
        node_cli.games(home, "ExampleGame.exe", False, say=print, interactive=lambda: False)
    node_cli.games(
        home, "ExampleGame.exe", False, say=print, interactive=lambda: True, confirm=lambda _p: True
    )
    assert (
        "ExampleGame.exe"
        not in effective_manifest(load_manifest(home.manifest_file), audit, key).games
    )
    with pytest.raises(node_cli.NodeCliError):
        node_cli.games(home, "C:/Games/x.exe", True, say=print)


def test_the_core_can_only_add_games() -> None:
    world, _ = _world()
    world.add_games(["ExampleGame.exe"])
    world.add_games([])
    assert world.extra_games == {"ExampleGame.exe"}


async def test_the_core_sends_its_games_on_every_connection() -> None:
    from nox.node.install import send_games

    sent: list[tuple[str, Any]] = []

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            sent.append((method, params))
            return {"ok": True}

    core = SimpleNamespace(
        config=SimpleNamespace(
            sensors=SimpleNamespace(game=SimpleNamespace(process_names=["RocketLeague.exe"]))
        )
    )
    await send_games(core, SimpleNamespace(client=Client(), device_id=LAPTOP))
    assert sent == [("nox/games", {"add": ["RocketLeague.exe"]})]


def test_the_wire_accepts_program_names_only() -> None:
    from nox.node.protocol import GamesAdd

    assert GamesAdd.model_validate({"add": ["Game One.exe"]}).add == ["Game One.exe"]
    for bad in (["../x.exe"], ["C:\\x.exe"], ["*"], ["x" * 70]):
        with pytest.raises(ValueError):
            GamesAdd.model_validate({"add": bad})


# ---- M1: the names and the order of the gate's checks --------------------------------------------


def test_the_locked_tools_are_exactly_these() -> None:
    assert (
        frozenset(
            {"computer.session_start", "computer.pointer", "computer.keys", "desktop.type_text"}
        )
        == INPUT_TOOLS
    )
    assert LOCKED_TOOLS == INPUT_TOOLS | {
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.window_close",
        "desktop.process_stop",
    }


def test_the_game_lock_answers_before_the_stop_and_the_receipt(tmp_path: Path) -> None:
    world, _ = _world(game="RocketLeague.exe")
    gate, _signer = _gate(world, tmp_path)
    gate.stop_state = lambda: "stopped_by_core"
    assert gate.check("computer.keys", {"combo": "ctrl+s"}).reason == "game_running"  # no receipt
    gate.stop_state = lambda: None
    assert gate.check("desktop.window_close", {"title": "x"}).reason == "game_running"


def test_listing_windows_stays_allowed_during_a_game(tmp_path: Path) -> None:
    world, _ = _world(game="RocketLeague.exe")
    gate, _signer = _gate(world, tmp_path)
    assert gate.check("desktop.windows", {}).allowed
    assert gate.check("desktop.processes", {}).allowed


def test_a_same_generation_pong_leaves_a_start_in_flight() -> None:
    control = NodeControl(None, lambda _n: None)
    control.observe(LAPTOP, _report(5, False))
    device = control._device(LAPTOP, "Laptop")
    from nox.node.world_routes import _Attempt

    device.attempt = _Attempt(control._clock(), device.gen)
    control.observe(LAPTOP, _report(5, False))  # a pong sent before the start was handled
    assert device.attempt is not None and control.blocks(BROWSER)
    control.observe(LAPTOP, _report(6, True))  # a newer generation settles it
    assert device.attempt is None and control.blocks(BROWSER)


# ---- H2/H3: approvals while the lock holds -------------------------------------------------------


class Gate:
    def __init__(self, *, required: bool = True, pin: str = "4711") -> None:
        self.required, self.pin = required, pin

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        if pin != self.pin:
            raise PinRequiredError(action, f"{action} requires the security PIN")


def _locked_control() -> NodeControl:
    control = NodeControl(None, lambda _n: None)
    control.observe(LAPTOP, _report(2, True))
    return control


def _ctx(role: str, client: str) -> Any:
    return SimpleNamespace(role=role, client_id=client)


@pytest.mark.parametrize(
    ("role", "pin", "remote", "gate", "expected"),
    [
        ("device", "4711", False, Gate(), REFUSE_DEVICE),  # a paired device: never
        ("dashboard", "4711", False, Gate(), None),  # this computer, with the PIN
        ("shell", "4711", False, Gate(), None),
        ("dashboard", None, False, Gate(), "security.permission.reply requires the security PIN"),
        ("dashboard", "4711", True, Gate(), REFUSE_REMOTE),  # a remote desktop
        ("dashboard", "4711", None, Gate(), REFUSE_REMOTE),  # cannot be told: refused
        ("dashboard", "4711", False, Gate(required=False), REFUSE_NO_PIN),
        ("dashboard", "4711", False, None, REFUSE_NO_PIN),
        ("plugin", "4711", False, Gate(), REFUSE_DEVICE),  # nothing else approves either
    ],
)
async def test_who_may_approve_while_nox_controls_a_node(
    role: str, pin: str | None, remote: bool | None, gate: Any, expected: str | None
) -> None:
    control = _locked_control()
    refusal = await approval_refusal(
        control, _ctx(role, f"{role}:1"), pin, gate=gate, remote=lambda: remote
    )
    assert refusal == expected


async def test_without_a_session_nothing_changes() -> None:
    control = NodeControl(None, lambda _n: None)
    assert (
        await approval_refusal(
            control, _ctx("device", "device:x"), None, gate=None, remote=lambda: True
        )
        is None
    )


async def test_the_reply_handler_refuses_a_paired_allow_and_keeps_deny() -> None:
    from nox.ipc.dispatch import RequestRegistry
    from nox.ipc.handlers.core import CoreHandlers, PermissionReply
    from nox.security.model import Decision

    replies: list[tuple[str, Decision]] = []
    engine = SimpleNamespace(
        reply=lambda gid, decision, **_kw: replies.append((gid, decision)) or True,
        pending_request=lambda _gid: None,
    )
    control = _locked_control()
    remote: dict[str, bool | None] = {"value": False}

    async def guard(ctx: Any, pin: str | None) -> str | None:
        return await approval_refusal(
            control, ctx, pin, gate=Gate(), remote=lambda: remote["value"]
        )

    core = SimpleNamespace(
        registry=RequestRegistry(),
        security=SimpleNamespace(engine=engine),
        node_approval_guard=guard,
        device_pin_guard=None,
    )
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    device = _ctx("device", "device:" + "e" * 32)
    local = _ctx("dashboard", "dashboard:1")
    allow = PermissionReply(grant_id="g", decision=Decision.ALLOW, pin="4711")
    deny = PermissionReply(grant_id="g", decision=Decision.DENY)
    assert (await handlers.permission_reply(device, allow))["ok"] is False
    assert replies == []  # the engine never saw the paired device's "allow"
    assert (await handlers.permission_reply(device, deny))["ok"] is True
    no_pin = PermissionReply(grant_id="g", decision=Decision.ALLOW)
    assert (await handlers.permission_reply(local, no_pin))["ok"] is False
    assert (await handlers.permission_reply(local, allow))["ok"] is True
    remote["value"] = True
    assert (await handlers.permission_reply(local, allow))["ok"] is False
    assert replies == [("g", Decision.DENY), ("g", Decision.ALLOW)]


def test_a_remote_session_is_recognised_on_linux() -> None:
    from nox.node.console import console_is_remote

    def run(out: str) -> Any:
        return lambda _argv: out

    assert console_is_remote(platform="linux", env={"SSH_CONNECTION": "x"}, run=run("")) is True
    local = {"XDG_SESSION_ID": "2"}
    assert console_is_remote(platform="linux", env=local, run=run("no\n")) is False
    assert console_is_remote(platform="linux", env=local, run=run("yes\n")) is True
    assert console_is_remote(platform="linux", env=local, run=run("")) is None
    assert console_is_remote(platform="linux", env={}, run=run("no")) is None  # a service


# ---- M3: a cancelled start -----------------------------------------------------------------------


async def test_a_cancelled_start_ends_the_session_in_the_background(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            raise asyncio.CancelledError
        return _answer_with(_report(9, False), was_open=True)

    specs, _reg, control, (_granted, _notices, ends) = _specs(tmp_path, _privacy(), answer)
    with pytest.raises(asyncio.CancelledError):
        await _start(specs)
    assert control.blocks(BROWSER)  # until the node says otherwise
    for _ in range(10):
        await asyncio.sleep(0)
    assert ends == [LAPTOP] and not control.blocks(BROWSER)


# ---- M4: a call that waits for the device's dialog -----------------------------------------------


async def test_a_high_call_waits_for_the_dialog_on_the_device() -> None:
    from nox.node.confirm import CONFIRM_TIMEOUT_S
    from nox.node.registry import LISTING_WAIT_S

    asked: list[float] = []

    class Client:
        closed = False

        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            asked.append(timeout_s)
            return {"content": [], "structuredContent": {"ok": True}}

    registry = NodeRegistry(SimpleNamespace(), SimpleNamespace())  # type: ignore[arg-type]
    entry = NodeEntry(device_id=LAPTOP, name="Laptop", online=True, tools={"shell.run": "high"})
    entry.session = SimpleNamespace(alive=True, client=Client(), take_blob=lambda _c: None)  # type: ignore[assignment]
    await registry.call(entry, "shell.run", {}, risk="high", timeout_s=30)
    await registry.call(entry, "shell.run", {}, risk="low", timeout_s=30)
    assert asked == [30 + CONFIRM_TIMEOUT_S, 30]
    assert LISTING_WAIT_S < ROUTED_TIMEOUT_S


def test_the_executor_waits_as_long_as_a_routed_call(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return {}

    specs, *_ = _specs(tmp_path, _privacy(), answer)
    assert all(spec.timeout_s >= ROUTED_TIMEOUT_S for spec in specs.values())


# ---- M5 and the captures -------------------------------------------------------------------------


async def test_privacy_switched_on_during_the_dialog_discards_the_picture(tmp_path: Path) -> None:
    privacy = _privacy()

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        # The person at the device takes their time; meanwhile panic mode switches capture off.
        privacy.allows_capture = lambda _kind: False
        meta = {"ok": True, "sha256": hashlib.sha256(PNG).hexdigest()}
        return {"device": entry.label, "result": meta, BLOB_RESULT_KEY: PNG}

    specs, *_ = _specs(tmp_path, privacy, answer)
    result = await _route(specs, "screen.capture", {})
    assert result["error"] == "capture.refused"
    assert not (tmp_path / "captures").exists() or not any((tmp_path / "captures").rglob("*.png"))


async def test_a_large_screen_is_made_smaller_on_the_node() -> None:
    world, _ = _world()
    huge = PNG + b"x" * (9 * 1024 * 1024)
    world.screen = lambda: _bytes(huge)
    world.fit = lambda _png: PNG + b"small"
    tools = world_tools(world)
    result = await tools["screen.capture"].handler(tools["screen.capture"].args())
    assert result["_blob"] == PNG + b"small"
    world.fit = lambda _png: None  # cannot be made smaller: refused, never cut
    result = await tools["screen.capture"].handler(tools["screen.capture"].args())
    assert result == {"ok": False, "error": "capture.too_large", "bytes": len(huge)}


async def _bytes(data: bytes) -> bytes:
    return data


async def test_window_titles_from_a_node_are_hidden_in_panic(tmp_path: Path) -> None:
    privacy = _privacy()
    privacy.panic = False
    privacy.snapshot = lambda: SimpleNamespace(safe_mode=False)
    window = {"handle": 7, "title": "Bank - Kontostand", "process": "browser.exe"}

    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return {"device": entry.label, "result": {"ok": True, "windows": [dict(window)]}}

    laptop_tools = {"desktop.windows": "read"}
    specs, registry, *_ = _specs(tmp_path, privacy, answer)
    registry._entries[0].tools.update(laptop_tools)  # type: ignore[attr-defined]
    shown = await _route(specs, "desktop.windows", {})
    assert shown["result"]["windows"][0]["title"] == "Bank - Kontostand"
    privacy.panic = True
    hidden = await _route(specs, "desktop.windows", {})
    assert hidden["result"]["windows"][0]["title"] == "[hidden: privacy]"


# ---- the smaller points --------------------------------------------------------------------------


def test_a_new_hello_starts_a_new_epoch() -> None:
    control = NodeControl(None, lambda _n: None)
    control.observe(LAPTOP, _report(2**50, True))
    control.observe(LAPTOP, _report(7, False))  # a lower generation: ignored ...
    assert control.blocks(BROWSER)
    control.hello(LAPTOP, _report(7, False))  # ... unless it is a new run's hello
    assert not control.blocks(BROWSER)
    control.hello(LAPTOP, None)  # a hello without a report changes nothing
    control.observe(LAPTOP, _report(8, True))
    control.hello(LAPTOP, "broken")
    assert control.blocks(BROWSER)


def test_the_epoch_is_random_per_run() -> None:
    first, _ = _world()
    second, _ = _world()
    assert first.gen != second.gen and first.gen < 2**62


async def test_grants_say_where_they_come_from(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return _answer_with(_report(2, True), minutes=5)

    specs, _reg, _control, (granted, *_rest) = _specs(tmp_path, _privacy(), answer)
    await _start(specs)
    assert granted[0].granted_by == "node-session"


def test_the_lock_never_holds_longer_than_the_longest_session() -> None:
    now = {"t": 0.0}
    control = NodeControl(None, lambda _n: None, clock=lambda: now["t"])
    control.observe(LAPTOP, _report(2, True))
    for _ in range(3):  # a node that keeps saying "open" for hours
        now["t"] += MAX_SESSION_MINUTES * 30
        control.observe(LAPTOP, _report(2, True))
    assert not control.blocks(BROWSER)


def test_the_owner_releases_a_stuck_lock_and_a_live_session_locks_again() -> None:
    notices: list[dict[str, Any]] = []
    control = NodeControl(None, notices.append)
    control.observe(LAPTOP, _report(2, True))
    assert control.status() and control.status()[0]["device_id"] == LAPTOP
    assert control.release("released by the owner") == 1
    assert not control.blocks(BROWSER) and notices[-1]["active"] is False
    assert control.status() == []
    control.observe(LAPTOP, _report(2, True))  # the node still says it runs
    assert control.blocks(BROWSER)


async def test_the_release_request_needs_the_pin_here(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.ipc.dispatch import RequestRegistry
    from nox.ipc.errors import IpcError
    from nox.node.install import ReleaseRequest, _register_control_requests

    audit: list[str] = []
    ipc = RequestRegistry()
    control = _locked_control()
    core = SimpleNamespace(
        security=SimpleNamespace(
            audit=SimpleNamespace(append=lambda **kw: audit.append(kw["decision"]))
        )
    )
    _register_control_requests(core, ipc, control, lambda: Gate())
    handler = ipc._handlers["node.computer.release"].handler
    import nox.node.install as install_module

    monkeypatch.setattr(install_module, "console_is_remote", lambda: False)
    with pytest.raises(IpcError):
        await handler(_ctx("dashboard", "dashboard:1"), ReleaseRequest(pin="0000"))
    done = await handler(_ctx("dashboard", "dashboard:1"), ReleaseRequest(pin="4711"))
    assert done["released"] == 1 and audit == ["deny", "allow"]
    assert ipc._handlers["node.computer.release"].allowed_roles == frozenset({"shell", "dashboard"})


async def test_music_is_announced_once_on_the_device(tmp_path: Path) -> None:
    shown: list[str] = []

    class Notifier:
        async def show(self, title: str, text: str) -> None:
            shown.append(text)

    home = NodeHome(tmp_path / "node")
    node_cli.confirm_defaults(home, None)
    runtime = node_cli.build(home, notifier=Notifier())
    assert await node_cli.media_notice_once(runtime) is True
    assert await node_cli.media_notice_once(runtime) is False
    assert shown == [node_cli.MEDIA_NOTICE]
