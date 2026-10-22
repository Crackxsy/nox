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
    REFUSE_LINKED,
    REFUSE_NO_PIN,
    REFUSE_REMOTE,
    REFUSE_ROLE,
    NodeControl,
    session_refusal,
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
    assert device.attempt is not None and control.active()
    control.observe(LAPTOP, _report(6, True))  # a newer generation settles it
    assert device.attempt is None and control.active()


# ---- H2/H3/M-a/M-b: approvals and changes while the lock holds ----------------------------------

OTHER_PHONE = "e" * 32


class Gate:
    def __init__(self, *, required: bool = True, pin: str = "4711") -> None:
        self.required, self.pin = required, pin

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        if pin != self.pin:
            raise PinRequiredError(action, f"{action} requires the security PIN")


class DeviceGuard:
    """The paired-device PIN check (`DevicePinGuard`): the same PIN, its own lockout."""

    def __init__(self, gate: Gate) -> None:
        self.gate, self.wrong = gate, 0

    async def require(self, ctx: Any, pin: str | None, action: str) -> None:
        from nox.ipc.errors import IpcError

        try:
            await self.gate.require(pin, action=action, by=ctx.client_id)
        except PinRequiredError as exc:
            self.wrong += 1
            raise IpcError("permission.denied", exc.reason) from exc


def _locked_control() -> NodeControl:
    control = NodeControl(None, lambda _n: None, linked=lambda d: BROWSER if d == LAPTOP else None)
    control.observe(LAPTOP, _report(2, True))
    return control


def _ctx(role: str, client: str) -> Any:
    return SimpleNamespace(role=role, client_id=client)


@pytest.mark.parametrize(
    ("role", "client", "pin", "remote", "gate", "expected"),
    [
        # 1. the browser on the PC Nox controls: never, not even with the PIN
        ("device", "device:" + BROWSER, "4711", False, Gate(), REFUSE_LINKED),
        # 2. any other paired device and this machine: with the PIN
        ("device", "device:" + OTHER_PHONE, "4711", False, Gate(), None),
        ("device", "device:" + OTHER_PHONE, None, False, Gate(), "x requires the security PIN"),
        ("dashboard", "dashboard:1", "4711", False, Gate(), None),
        ("shell", "shell:1", "4711", False, Gate(), None),
        ("dashboard", "dashboard:1", None, False, Gate(), "x requires the security PIN"),
        # 3. this machine from a remote session, or when that cannot be told: refused
        ("dashboard", "dashboard:1", "4711", True, Gate(), REFUSE_REMOTE),
        ("dashboard", "dashboard:1", "4711", None, Gate(), REFUSE_REMOTE),
        # 4. no PIN set: refused for everyone
        ("dashboard", "dashboard:1", "4711", False, Gate(required=False), REFUSE_NO_PIN),
        ("device", "device:" + OTHER_PHONE, "4711", False, Gate(required=False), REFUSE_NO_PIN),
        ("dashboard", "dashboard:1", "4711", False, None, REFUSE_NO_PIN),
        ("plugin", "plugin:1", "4711", False, Gate(), REFUSE_ROLE),
    ],
)
async def test_who_may_approve_or_change_while_nox_controls_a_node(
    role: str, client: str, pin: str | None, remote: bool | None, gate: Any, expected: str | None
) -> None:
    refusal = await session_refusal(
        _locked_control(),
        _ctx(role, client),
        pin,
        action="x",
        gate=gate,
        device_guard=DeviceGuard(gate) if gate is not None else None,
        remote=lambda: remote,
    )
    assert refusal == expected


async def test_without_a_session_nothing_is_asked() -> None:
    control = NodeControl(None, lambda _n: None)
    refusal = await session_refusal(
        control,
        _ctx("device", "device:" + BROWSER),
        None,
        action="x",
        gate=None,
        device_guard=None,
        remote=lambda: True,
    )
    assert refusal is None


async def test_a_slow_remote_check_never_blocks_and_counts_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_module

    from nox.node import world_routes

    monkeypatch.setattr(world_routes, "REMOTE_CHECK_S", 0.05)

    def slow() -> bool:
        time_module.sleep(0.3)
        return False

    assert await world_routes.remote_now(slow) is None


def _wired(gate: Gate) -> tuple[Any, NodeControl, DeviceGuard, list[str]]:
    """A core wired exactly like `nox.node.install` (`wire_guards`), with fakes behind it."""
    from nox.ipc.dispatch import RequestRegistry
    from nox.node.install import wire_guards

    device_guard = DeviceGuard(gate)
    audit: list[str] = []
    core = SimpleNamespace(
        registry=RequestRegistry(),
        security=SimpleNamespace(
            gate=gate,
            engine=SimpleNamespace(
                reply=lambda gid, decision, **_kw: audit.append(f"{gid}:{decision.value}") or True,
                pending_request=lambda _gid: None,
            ),
            audit=SimpleNamespace(append=lambda **kw: audit.append(kw["decision"])),
        ),
        device_pin_guard=device_guard,
    )
    control = _locked_control()
    wire_guards(core, control)
    return core, control, device_guard, audit


def _envelope(name: str, role: str, client: str, payload: dict[str, Any]) -> Any:
    from nox.ipc.protocol import Envelope, Kind, Source

    return Envelope(kind=Kind.REQUEST, name=name, src=Source(role=role, id=client), payload=payload)  # type: ignore[arg-type]


async def test_changes_go_through_the_same_rule_on_the_real_dispatch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-a: not only "allow" - adding an MCP server, approving a plan, any change."""
    from pydantic import BaseModel

    import nox.node.install as install_module
    from nox.ipc.dispatch import RequestContext
    from nox.ipc.protocol import Kind

    monkeypatch.setattr(install_module, "console_is_remote", lambda: False)
    core, control, _guard, _audit = _wired(Gate())
    ran: list[tuple[str, dict[str, Any]]] = []

    class Add(BaseModel):
        model_config = {"extra": "forbid"}
        command: str = "x"

    def handler(name: str) -> Any:
        async def run(_ctx: Any, p: Any) -> dict[str, Any]:
            ran.append((name, p.model_dump()))
            return {}

        return run

    roles = ("dashboard", "shell", "device")
    core.registry.register("mcp.servers.add", Add, handler("mcp.servers.add"), roles=roles)
    core.registry.register("plans.approve", Add, handler("plans.approve"), roles=roles)
    core.registry.register("state.get", Add, handler("state.get"), roles=roles)

    async def send(name: str, role: str, client: str, payload: dict[str, Any]) -> Any:
        envelope = _envelope(name, role, client, payload)
        ctx = RequestContext(client_id=client, role=role, request=envelope)
        return await core.registry.dispatch(ctx, envelope)

    local, phone, linked = "dashboard:1", "device:" + OTHER_PHONE, "device:" + BROWSER
    assert (await send("mcp.servers.add", "dashboard", local, {})).kind is Kind.ERROR
    assert (await send("plans.approve", "device", phone, {})).kind is Kind.ERROR
    assert (await send("plans.approve", "device", linked, {"pin": "4711"})).kind is Kind.ERROR
    assert ran == []
    # With the PIN: through, and the PIN is not passed on to a request that takes none.
    assert (
        await send("mcp.servers.add", "dashboard", local, {"pin": "4711"})
    ).kind is Kind.RESPONSE
    assert (await send("plans.approve", "device", phone, {"pin": "4711"})).kind is Kind.RESPONSE
    assert ran == [("mcp.servers.add", {"command": "x"}), ("plans.approve", {"command": "x"})]
    # Reading never asks.
    assert (await send("state.get", "device", linked, {})).kind is Kind.RESPONSE
    # Once the node says the session is over, nothing is asked any more.
    control.observe(LAPTOP, _report(3, False))
    assert (await send("plans.approve", "device", phone, {})).kind is Kind.RESPONSE


async def test_the_reply_handler_follows_the_rule_and_deny_always_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nox.node.install as install_module
    from nox.ipc.handlers.core import CoreHandlers, PermissionReply
    from nox.security.model import Decision

    remote: dict[str, bool | None] = {"value": False}
    monkeypatch.setattr(install_module, "console_is_remote", lambda: remote["value"])
    core, _control, guard, audit = _wired(Gate())
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    linked = _ctx("device", "device:" + BROWSER)
    phone = _ctx("device", "device:" + OTHER_PHONE)
    local = _ctx("dashboard", "dashboard:1")
    allow = PermissionReply(grant_id="g", decision=Decision.ALLOW, pin="4711")
    no_pin = PermissionReply(grant_id="g", decision=Decision.ALLOW)
    deny = PermissionReply(grant_id="g", decision=Decision.DENY)
    assert (await handlers.permission_reply(linked, allow))["ok"] is False  # never
    assert audit == []  # the engine never saw it
    assert (await handlers.permission_reply(linked, deny))["ok"] is True  # deny always
    assert (await handlers.permission_reply(phone, no_pin))["ok"] is False
    assert guard.wrong == 1  # counted towards the device's lockout
    assert (await handlers.permission_reply(phone, allow))["ok"] is True
    assert (await handlers.permission_reply(local, no_pin))["ok"] is False
    assert (await handlers.permission_reply(local, allow))["ok"] is True
    remote["value"] = True
    assert (await handlers.permission_reply(local, allow))["ok"] is False
    assert audit == ["g:deny", "g:allow", "g:allow"]


async def test_the_real_wiring_registers_status_release_and_the_hello_reset() -> None:
    """N31: `wire_guards`, `_register_control_requests` and the connection hook are wired."""
    from nox.node.install import connection_hook

    core, control, _guard, _audit = _wired(Gate())
    assert core.registry.get("node.computer.status") is not None
    assert core.registry.get("node.computer.release") is not None
    assert core.registry.node_guard is not None and core.node_approval_guard is not None
    connected: list[str] = []

    class Registry:
        async def on_connected(self, session: Any) -> None:
            connected.append(session.device_id)

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            connected.append(method)
            return {}

    core.config = SimpleNamespace(
        sensors=SimpleNamespace(game=SimpleNamespace(process_names=["RocketLeague.exe"]))
    )
    hook = connection_hook(core, Registry(), [control])  # type: ignore[arg-type]
    session = SimpleNamespace(device_id=LAPTOP, computer=_report(1, False), client=Client())
    await hook(session)
    assert connected == [LAPTOP, "nox/games"]
    assert not control.locked(LAPTOP)  # the new run's hello reset the old session


def test_a_remote_session_is_recognised() -> None:
    from nox.node.console import console_is_remote

    def run(out: str) -> Any:
        return lambda _argv: out

    linux = "linux"
    assert console_is_remote(platform=linux, env={"SSH_CONNECTION": "x"}, run=run("")) is True
    local = {"XDG_SESSION_ID": "2"}
    assert console_is_remote(platform=linux, env=local, run=run("no\n")) is False
    assert console_is_remote(platform=linux, env=local, run=run("yes\n")) is True
    assert console_is_remote(platform=linux, env=local, run=run("")) is None
    # A service without a login session (the Pi image): nothing remote about it; the PIN applies.
    assert console_is_remote(platform=linux, env={}, run=run("no")) is False


class _WinDll:
    def __init__(self, *, remote: int = 0, session: int = 1, console: int = 1) -> None:
        self.user32 = SimpleNamespace(GetSystemMetrics=lambda _m: remote)
        self.kernel32 = SimpleNamespace(
            ProcessIdToSessionId=self._session, WTSGetActiveConsoleSessionId=lambda: console
        )
        self._value = session

    def _session(self, _pid: int, out: Any) -> int:
        out._obj.value = self._value
        return 1


def test_a_remote_session_is_recognised_on_windows() -> None:
    from nox.node.console import SM_REMOTESESSION, console_is_remote

    assert SM_REMOTESESSION == 0x1000
    assert console_is_remote(platform="win32", windll=_WinDll(remote=1)) is True  # N7
    assert console_is_remote(platform="win32", windll=_WinDll()) is False  # at the console
    assert console_is_remote(platform="win32", windll=_WinDll(session=2)) is True  # not it
    # N8: no session attached to the console: unknown - never "local".
    assert console_is_remote(platform="win32", windll=_WinDll(console=0xFFFFFFFF)) is None


def test_a_service_query_that_fails_locks() -> None:
    """N19: only "no such service" means not running; any other failure raises (locked)."""
    from nox.desktop.anticheat import ERROR_SERVICE_DOES_NOT_EXIST, running_services

    errors = {"missing": ERROR_SERVICE_DOES_NOT_EXIST, "denied": 5}
    last: list[int] = [0]

    def open_service(_manager: Any, name: str, _access: int) -> int:
        if name in errors:
            last[0] = errors[name]
            return 0
        return 7

    def query(_service: Any, status: Any) -> int:
        status._obj.dwCurrentState = 4  # running
        return 1

    advapi = SimpleNamespace(
        OpenSCManagerW=lambda *_a: 1,
        OpenServiceW=open_service,
        QueryServiceStatus=query,
        CloseServiceHandle=lambda _h: 1,
    )
    api = (advapi, lambda: last[0])
    assert running_services(["missing"], api=api) == set()
    assert running_services(["vgk"], api=api) == {"vgk"}
    with pytest.raises(OSError):
        running_services(["denied"], api=api)
    assert _lock(services=lambda: running_services(["denied"], api=api)).reason() == "game_running"
    no_manager = SimpleNamespace(**{**advapi.__dict__, "OpenSCManagerW": lambda *_a: 0})
    with pytest.raises(OSError):
        running_services(["vgk"], api=(no_manager, lambda: 5))


def test_a_store_app_in_front_does_not_lock_but_exclusive_full_screen_does() -> None:
    assert _lock(screen_state=lambda: 7).reason() is None  # QUNS_APP
    assert _lock(screen_state=lambda: 3).reason() == "game_in_foreground"


def test_more_anti_cheat_services_are_known() -> None:
    from nox.desktop.anticheat import ANTI_CHEAT_SERVICES

    for name in ("ESEADriver2", "denuvo-anticheat", "EQU8", "NeacSafe64"):
        assert name in ANTI_CHEAT_SERVICES


def test_removing_a_game_without_a_confirmation_writes_nothing(tmp_path: Path) -> None:
    from nox.node.manifest import NodeManifest, save_manifest

    home = NodeHome(tmp_path / "node")
    home.ensure()
    save_manifest(home.manifest_file, NodeManifest(games=["ExampleGame.exe"]))
    before = home.manifest_file.read_text(encoding="utf-8")
    with pytest.raises(node_cli.NodeCliError):
        node_cli.games(
            home,
            "ExampleGame.exe",
            False,
            say=print,
            interactive=lambda: True,
            confirm=lambda _p: True,
        )
    assert home.manifest_file.read_text(encoding="utf-8") == before


async def test_the_core_sends_its_games_again_when_they_change() -> None:
    from nox.node.install import resend_games_if_changed

    sent: list[Any] = []

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            sent.append(params["add"])
            return {}

    names = ["RocketLeague.exe"]
    core = SimpleNamespace(
        config=SimpleNamespace(sensors=SimpleNamespace(game=SimpleNamespace(process_names=names)))
    )
    listener = SimpleNamespace(
        sessions={LAPTOP: SimpleNamespace(client=Client(), device_id=LAPTOP)}
    )
    known: list[list[str]] = [["RocketLeague.exe"]]
    assert await resend_games_if_changed(core, listener, known) is False
    names.append("ExampleGame.exe")
    assert await resend_games_if_changed(core, listener, known) is True
    assert sent == [["RocketLeague.exe", "ExampleGame.exe"]]


# ---- M3: a cancelled start -----------------------------------------------------------------------


async def test_a_cancelled_start_ends_the_session_in_the_background(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        if tool == "computer.session_start":
            raise asyncio.CancelledError
        return _answer_with(_report(9, False), was_open=True)

    specs, _reg, control, (_granted, _notices, ends) = _specs(tmp_path, _privacy(), answer)
    with pytest.raises(asyncio.CancelledError):
        await _start(specs)
    assert control.active()  # until the node says otherwise
    for _ in range(10):
        await asyncio.sleep(0)
    assert ends == [LAPTOP] and not control.active()


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


def test_the_executor_waits_longer_only_for_a_call_on_a_node(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return {}

    specs, *_ = _specs(tmp_path, _privacy(), answer)
    for spec in specs.values():
        assert spec.call_timeout_s is not None
        assert spec.call_timeout_s({"device": LAPTOP}) >= ROUTED_TIMEOUT_S
        assert spec.call_timeout_s({"device": "core"}) is None  # the core's own limit
        assert spec.call_timeout_s({"device": ""}) is None


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
    assert control.active()
    control.hello(LAPTOP, _report(7, False))  # ... unless it is a new run's hello
    assert not control.active()
    control.hello(LAPTOP, None)  # a hello without a report changes nothing
    control.observe(LAPTOP, _report(8, True))
    control.hello(LAPTOP, "broken")
    assert control.active()


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


def _clocked() -> tuple[NodeControl, dict[str, float], list[Any]]:
    now = {"t": 0.0}
    granted: list[Any] = []
    engine = SimpleNamespace(grant_temporary=granted.append, revoke=granted.append)
    control = NodeControl(engine, lambda _n: None, clock=lambda: now["t"])
    return control, now, granted


def test_a_long_session_that_keeps_reporting_keeps_the_lock() -> None:
    control, now, _ = _clocked()
    control.observe(LAPTOP, _report(2, True))
    for _ in range(3 * 60):  # three hours of reports, one a minute
        now["t"] += 60
        control.observe(LAPTOP, _report(2, True))
        assert control.active()


def test_back_to_back_sessions_keep_the_lock_and_get_a_fresh_grant() -> None:
    control, now, granted = _clocked()
    control.observe(LAPTOP, _report(2, True, "aa" * 16))
    for gen in range(3, 30, 2):  # ended and started again, every few minutes, for hours
        now["t"] += MAX_SESSION_MINUTES * 6
        control.observe(LAPTOP, _report(gen, True, f"{gen:02x}" * 16))
        assert control.active()
    grants = [g for g in granted if not isinstance(g, str)]
    assert len({g.grant_id for g in grants}) == len(grants) > 1  # one per session


def test_a_silent_node_lifts_the_lock_after_the_longest_session() -> None:
    control, now, _ = _clocked()
    control.observe(LAPTOP, _report(2, True))
    now["t"] += MAX_SESSION_MINUTES * 60 - 1
    assert control.active()
    now["t"] += 2
    assert not control.active()


def test_the_owner_releases_a_stuck_lock_and_a_live_session_locks_again() -> None:
    notices: list[dict[str, Any]] = []
    control = NodeControl(None, notices.append)
    control.observe(LAPTOP, _report(2, True))
    assert control.status() and control.status()[0]["device_id"] == LAPTOP
    assert control.release("released by the owner") == 1
    assert not control.active() and notices[-1]["active"] is False
    assert control.status() == []
    control.observe(LAPTOP, _report(2, True))  # the node still says it runs
    assert control.active()


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


async def test_precheck_runs_before_around_and_stops_it() -> None:
    from nox.node.router import routed_spec
    from nox.node.tools import NotifyArgs
    from nox.security.model import Risk
    from tests.unit.node.test_router import FakeRegistry, _entry

    order: list[str] = []
    laptop = _entry(LAPTOP, "Laptop", tools={"notify.show": "low"})

    def precheck(payload: dict[str, Any]) -> str | None:
        order.append("precheck")
        return "refused_text" if payload.get("text") == "bad" else None

    async def around(entry: Any, args: dict[str, Any], call: Any) -> dict[str, Any]:
        order.append("around")
        return dict(await call())

    spec = routed_spec(
        name="notify.show",
        description="t",
        risk=Risk.LOW,
        side_effects=True,
        registry=FakeRegistry([laptop]),  # type: ignore[arg-type]
        input_model=NotifyArgs,
        precheck=precheck,
        around=around,
    )
    assert spec.scope is not None
    for text in ("bad", "gut"):
        payload = spec.input_model.model_validate({"text": text, "device": "Laptop"}).model_dump()
        if spec.prepare is not None:
            await spec.prepare(payload)
        spec.scope(payload)
        await spec.handler(payload)
    assert order == ["precheck", "precheck", "around"]
