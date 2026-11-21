"""The game lock: input on a node is refused while anything owns the screen or an
anti-cheat service runs, every check that cannot answer locks, and only the device can
remove a game from its own list - the core can only add."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.device import cli as node_cli
from nox.node.device.confirmed import effective_manifest
from nox.node.device.gate import NodeGate
from nox.node.device.home import NodeHome
from nox.node.device.world.catalogue import world_tools
from nox.node.device.world.game_lock import GameLock
from nox.node.wire.manifest import NodeManifest
from tests.unit.node.support_sessions import LAPTOP, device_world, world_gate, world_meta

# ---- the game lock -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "computer.session_start",
        "computer.pointer",
        "computer.keys",
        "desktop.type_text",
        "desktop.window_focus",
        "desktop.window_minimize",
        "desktop.window_restore",
        "desktop.window_close",
        "desktop.process_stop",
    ],
)
@pytest.mark.parametrize(("game", "front", "reason"), [
    ("RocketLeague.exe", "", "game_running"),
    ("EasyAntiCheat.exe", "", "game_running"),
    ("", "rocketleague.exe", "game_in_foreground"),
])  # fmt: skip
def test_input_is_refused_on_the_node_while_a_game_runs(
    name: str, game: str, front: str, reason: str, tmp_path: Path
) -> None:
    """Even a core that signs everything gets no input through while a game runs here."""
    world, seen = device_world(game=game, front=front)
    gate, signer = world_gate(world, tmp_path)
    args = {"title": "Editor", "text": "hallo"} if name == "desktop.type_text" else {}
    decision = gate.check(name, args, meta=world_meta(signer, name, args, "high"))
    assert not decision.allowed and decision.reason == reason
    assert seen["sender"].events == []


def test_the_game_lock_fails_closed_and_ending_a_session_is_never_locked(tmp_path: Path) -> None:
    world, _ = device_world(game="RocketLeague.exe")
    gate, signer = world_gate(world, tmp_path)
    meta = world_meta(signer, "computer.session_end", {}, "low")
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
    world, seen = device_world()
    world.lock.running_names = lambda: names
    tools = world_tools(world)
    started = await tools["computer.session_start"].handler(
        tools["computer.session_start"].args.model_validate({"purpose": "Datei speichern"})
    )
    assert started["ok"] and world.session_report()["active"]
    keys = tools["computer.keys"]
    assert (await keys.handler(keys.args.model_validate({"combo": "ctrl+s"})))["ok"]
    sent = len(seen["sender"].events)
    names.add("rocketleague.exe")  # the guard inside ComputerUse, before every single event
    refused = await keys.handler(keys.args.model_validate({"combo": "ctrl+s"}))
    assert refused["ok"] is False and world.session_report()["active"] is False
    assert len(seen["sender"].events) == sent


# ---- the game lock -------------------------------------------------------------------------------


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
    from nox.node.wire.manifest import load_manifest, save_manifest

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
    world, _ = device_world()
    world.add_games(["ExampleGame.exe"])
    world.add_games([])
    assert world.extra_games == {"ExampleGame.exe"}


async def test_the_core_sends_its_games_on_every_connection() -> None:
    from nox.node.hub.install.games import send_games

    sent: list[tuple[str, Any]] = []

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            sent.append((method, params))
            return {"ok": True}

    config = SimpleNamespace(
        sensors=SimpleNamespace(game=SimpleNamespace(process_names=["RocketLeague.exe"]))
    )
    await send_games(config, SimpleNamespace(client=Client(), device_id=LAPTOP))  # type: ignore[arg-type]
    assert sent == [("nox/games", {"add": ["RocketLeague.exe"]})]


def test_the_wire_accepts_program_names_only() -> None:
    from nox.node.wire.protocol import GamesAdd

    assert GamesAdd.model_validate({"add": ["Game One.exe"]}).add == ["Game One.exe"]
    for bad in (["../x.exe"], ["C:\\x.exe"], ["*"], ["x" * 70]):
        with pytest.raises(ValueError):
            GamesAdd.model_validate({"add": bad})


def test_a_service_query_that_fails_locks() -> None:
    """Only "no such service" means not running; any other failure raises (locked)."""
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
    from nox.node.wire.manifest import NodeManifest, save_manifest

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
    from nox.node.hub.install.games import resend_games_if_changed

    sent: list[Any] = []

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            sent.append(params["add"])
            return {}

    names = ["RocketLeague.exe"]
    config = SimpleNamespace(sensors=SimpleNamespace(game=SimpleNamespace(process_names=names)))
    listener = SimpleNamespace(
        sessions={LAPTOP: SimpleNamespace(client=Client(), device_id=LAPTOP)}
    )
    known: list[list[str]] = [["RocketLeague.exe"]]
    assert await resend_games_if_changed(config, listener, known) is False
    names.append("ExampleGame.exe")
    assert await resend_games_if_changed(config, listener, known) is True
    assert sent == [["RocketLeague.exe", "ExampleGame.exe"]]
