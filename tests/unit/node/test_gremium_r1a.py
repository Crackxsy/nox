"""Review panel, round 1, part A: security, protocol and the node's terminal.

H1 (a household frozen by a node that fakes computer use), H2 (computer use on a node and the
commands that widen it), M1-M3, K1 (protocol extensibility), and the terminal's words. Fakes
only: no real session, input, window, microphone or camera; every name and address is made up.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.protocol import NodeHello, Ping, SessionReport
from nox.node.world_routes import (
    REFUSE_SATURATED,
    SUB_LINKED,
    SUB_NO_PIN,
    SUB_PIN,
    SUB_SATURATED,
    NodeControl,
    Refusal,
    escape_refusal,
    session_refusal,
)
from tests.unit.node.test_c6 import BROWSER, LAPTOP, _report
from tests.unit.node.test_c6_followup import LOCAL, PHONE, _registry, _send_from
from tests.unit.node.test_c6_review import Gate, _ctx, _envelope

OTHER = "c" * 32


def _violations() -> tuple[NodeControl, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []
    control = NodeControl(
        None,
        lambda _n: None,
        linked=lambda d: BROWSER if d == LAPTOP else None,
        on_violation=lambda device, why: seen.append((device, why)),
    )
    return control, seen


# ---- H1 a: the lock comes only from a start of this core ---------------------------------------


def test_a_node_claiming_a_session_in_its_hello_is_a_violation_and_locks_nothing() -> None:
    control, seen = _violations()
    assert control.hello(LAPTOP, _report(9, True)) is False
    assert seen == [(LAPTOP, "session_in_hello")]
    assert not control.active() and not control.locked(LAPTOP)


def test_a_pong_claiming_a_session_the_core_never_started_locks_nothing() -> None:
    control, seen = _violations()
    assert control.hello(LAPTOP, _report(1, False))
    control.observe(LAPTOP, _report(2, True))  # "open" - but this core started nothing
    control.observe(LAPTOP, _report(3, True, "dd" * 16))
    assert not control.active()
    assert seen == [(LAPTOP, "session_not_started_by_core")]  # once per connection


def test_only_the_session_the_core_started_holds_the_lock() -> None:
    control, seen = _violations()
    control.hello(LAPTOP, _report(1, False))
    control.answered(LAPTOP, _report(2, True, "aa" * 16))  # the answer to this core's own start
    assert control.locked(LAPTOP)
    control.observe(LAPTOP, _report(2, True, "aa" * 16))  # its pongs keep it
    assert control.locked(LAPTOP) and seen == []
    control.observe(LAPTOP, _report(3, False))  # over
    control.observe(LAPTOP, _report(4, True, "aa" * 16))  # the same id again, unasked
    assert not control.active() and seen == [(LAPTOP, "session_not_started_by_core")]


def test_a_new_connection_forgets_the_started_session() -> None:
    control, seen = _violations()
    control.answered(LAPTOP, _report(2, True, "aa" * 16))
    assert control.hello(LAPTOP, _report(1, False))  # a new run: its sessions ended
    control.observe(LAPTOP, _report(2, True, "aa" * 16))
    assert not control.active() and seen


async def test_the_connection_hook_drops_a_node_whose_hello_claims_a_session() -> None:
    from nox.node.install import connection_hook

    control, _seen = _violations()
    dropped: list[tuple[str, str]] = []
    listed: list[str] = []

    class Listener:
        async def drop(self, device_id: str, why: str) -> bool:
            dropped.append((device_id, why))
            return True

    registry = SimpleNamespace(on_connected=lambda s: listed.append(s.device_id))
    hook = connection_hook(SimpleNamespace(), registry, [control], [Listener()])  # type: ignore[arg-type]
    session = SimpleNamespace(device_id=LAPTOP, computer=SessionReport(gen=4, active=True, id=""))
    await hook(session)
    assert dropped == [(LAPTOP, "session_in_hello")] and listed == []


def _world_control(sessions: dict[str, Any], tools: dict[str, dict[str, str]]) -> Any:
    from nox.node.install import _world_specs
    from nox.tools.registry import ToolRegistry

    dropped: list[tuple[str, str]] = []

    async def drop(device_id: str, why: str) -> bool:
        dropped.append((device_id, why))
        return True

    listener = SimpleNamespace(sessions=sessions, drop=drop)
    entries = [
        SimpleNamespace(
            device_id=d, session=s, tools=tools.get(d, {}), linked_device_id=BROWSER, label="Laptop"
        )
        for d, s in sessions.items()
    ]
    registry = SimpleNamespace(known=lambda: entries, entries=lambda: entries)
    core = SimpleNamespace(
        config=SimpleNamespace(desktop=None, paths=None),
        tool_registry=ToolRegistry(),
        security=None,
        bus=None,
    )
    _specs, control = _world_specs(core, registry, dict, [listener])  # type: ignore[arg-type]
    return control, registry, dropped


async def test_reports_of_a_node_without_computer_use_are_never_read_as_open() -> None:
    """H1 c: "open" only from a node offering `computer.session_start` on this connection."""
    session = SimpleNamespace(
        computer=SessionReport(gen=2, active=True, id="ab" * 16),
        peer_host="",
        reported_addresses=[],
    )
    control, _registry_, dropped = _world_control({LAPTOP: session}, {})
    assert not control.active() and dropped == []  # ignored - not even a violation
    session.computer = SessionReport(gen=3, active=False)
    assert not control.active()


async def test_the_core_closes_a_node_that_claims_a_session_it_never_started() -> None:
    session = SimpleNamespace(
        computer=SessionReport(gen=2, active=True, id="ab" * 16),
        peer_host="",
        reported_addresses=[],
    )
    control, registry, dropped = _world_control(
        {LAPTOP: session}, {LAPTOP: {"computer.session_start": "high"}}
    )
    assert not control.active()
    await asyncio.sleep(0)
    assert dropped == [(LAPTOP, "session_not_started_by_core")]
    assert registry.may_delegate(OTHER)  # wired: no session, every node's dialog may ask


async def test_the_listener_drops_a_node_for_a_violation(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from websockets.exceptions import ConnectionClosed

    from tests.unit.node.conftest import client_context, greet, open_node, pair_node

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    await greet(ws)
    for _ in range(50):
        if node.node_id in listener.sessions:
            break
        await asyncio.sleep(0.02)
    assert await listener.drop(node.node_id, "session_not_started_by_core")
    with pytest.raises(ConnectionClosed):
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    assert node.node_id not in listener.sessions


# ---- H1 d-g: getting out of a lock ---------------------------------------------------------------


def _saturated() -> NodeControl:
    control = NodeControl(None, lambda _n: None, linked=lambda d: BROWSER if d == LAPTOP else None)
    control.answered(LAPTOP, _report(2, True))
    control.saturated.add(LAPTOP)
    return control


async def test_saturation_binds_approvals_and_messages_only() -> None:
    control = _saturated()
    gate = Gate()
    shell = _ctx("shell", "shell:cli")

    async def refusal(action: str, pin: str | None = "4711") -> Refusal | None:
        return await session_refusal(
            control, shell, pin, action=action, gate=gate, device_guard=None, remote=lambda: False
        )

    assert await refusal("security.permission.reply") is REFUSE_SATURATED
    assert (await refusal("chat.send")) is REFUSE_SATURATED
    assert REFUSE_SATURATED.code == SUB_SATURATED
    assert await refusal("plans.approve") is None  # the PIN rule, not saturation
    assert await refusal("node.unpair", None) is None  # this machine's shell: no PIN
    assert await refusal("node.computer.release") is None
    assert await refusal("security.resume") is None
    wrong = await refusal("node.computer.release", "0000")
    assert wrong is not None and wrong.code == SUB_PIN


async def test_the_escapes_work_with_the_pin_even_remotely_and_from_the_linked_browser() -> None:
    control = _saturated()
    gate = Gate()
    from tests.unit.node.test_c6_review import DeviceGuard

    linked = SimpleNamespace(role="device", client_id="device:" + BROWSER, peer="192.0.2.5")
    for action in ("node.unpair", "node.computer.release", "security.resume"):
        assert (
            await escape_refusal(
                linked, "4711", action=action, gate=gate, device_guard=DeviceGuard(gate)
            )
            is None
        )  # noqa: E501
        refused = await escape_refusal(
            linked, None, action=action, gate=gate, device_guard=DeviceGuard(gate)
        )  # noqa: E501
        assert refused is not None and refused.code == SUB_PIN
    remote_shell = _ctx("shell", "shell:ssh")
    assert (
        await session_refusal(
            control,
            remote_shell,
            "4711",
            action="node.computer.release",
            gate=gate,
            device_guard=None,
            remote=lambda: True,  # an SSH session to a headless core
        )
        is None
    )
    no_pin = await escape_refusal(
        remote_shell, None, action="security.resume", gate=Gate(required=False), device_guard=None
    )
    assert no_pin is not None and no_pin.code == SUB_NO_PIN


def test_removing_a_node_lifts_its_lock_binding_and_saturation_at_once() -> None:
    control = _saturated()
    control.node_reported[LAPTOP] = ["10.0.0.1"]
    assert control.active() and control.is_saturated()
    assert control.forget(LAPTOP) is True
    assert not control.active() and not control.is_saturated()
    assert LAPTOP not in control.node_reported and LAPTOP not in control.devices


async def test_release_and_unpair_pass_the_dispatch_guard_while_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.ipc.protocol import Kind
    from tests.unit.node.test_c6_followup import Text

    core, control, ran = _registry(monkeypatch)
    control.saturated.add(LAPTOP)

    async def unpair(_ctx: Any, _p: Any) -> dict[str, Any]:
        ran.append("node.unpair")
        return {}

    core.registry.register("node.unpair", Text, unpair, roles=("dashboard", "shell", "device"))
    reply = await _send_from(core, "node.unpair", LOCAL, {}, "")
    assert reply.kind is Kind.RESPONSE and ran == ["node.unpair"]
    released = await _send_from(core, "node.computer.release", LOCAL, {"pin": "4711"}, "")
    assert released.kind is Kind.RESPONSE and released.payload["released"] == 1
    refused = await _send_from(core, "node.computer.release", LOCAL, {"pin": "0000"}, "")
    assert refused.kind is Kind.ERROR
    assert refused.payload["details"]["sub_reason"] == SUB_PIN


async def test_a_refused_request_carries_a_stable_sub_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.ipc.protocol import Kind

    core, _control, _ran = _registry(monkeypatch)
    linked = await _send_from(core, "plans.approve", "device:" + BROWSER, {"pin": "4711"}, "")
    assert linked.kind is Kind.ERROR
    assert linked.payload["details"] == {"reason": "node_session", "sub_reason": SUB_LINKED}
    assert linked.payload["message"].isascii()  # the English fallback; the dashboard words it
    phone = await _send_from(core, "plans.approve", PHONE, {}, "")
    assert phone.payload["details"]["sub_reason"] == SUB_PIN


async def test_the_input_lock_counts_unmuting_as_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """H2 (architecture): one rule for "calm" with the node lock."""
    from nox.ipc.dispatch import RequestContext, RequestRegistry
    from nox.ipc.protocol import Kind
    from tests.unit.node.test_c6_followup import Mute

    registry = RequestRegistry()

    async def mute(_ctx: Any, _p: Any) -> dict[str, Any]:
        return {}

    registry.register("voice.mute", Mute, mute, roles=("dashboard",))
    registry.input_lock = lambda: True
    for muted, kind in ((True, Kind.RESPONSE), (False, Kind.ERROR)):
        envelope = _envelope("voice.mute", "dashboard", LOCAL, {"muted": muted})
        ctx = RequestContext(client_id=LOCAL, role="dashboard", request=envelope)
        assert (await registry.dispatch(ctx, envelope)).kind is kind


# ---- M1 / M2: which device -----------------------------------------------------------------------


def test_while_a_session_runs_only_its_own_node_may_ask_for_high_risk() -> None:
    control = NodeControl(None, lambda _n: None)
    assert control.may_delegate(OTHER)
    control.answered(LAPTOP, _report(2, True))
    assert control.may_delegate(LAPTOP) and not control.may_delegate(OTHER)


async def test_package_tools_refuse_a_paired_device_instead_of_running_here() -> None:
    from nox.hands.packages import register_package_tools
    from nox.node.router import NOT_AVAILABLE_ON_DEVICE, core_only_spec
    from nox.tools.registry import ToolRegistry

    ran: list[list[str]] = []

    async def run(argv: list[str]) -> tuple[int, str, str]:
        ran.append(argv)
        return 0, "", ""

    tools = ToolRegistry()
    register_package_tools(tools, run)
    own = tools.get("package.install")
    assert own is not None
    entries: list[Any] = [SimpleNamespace(device_id=LAPTOP, linked_device_id=None)]
    registry = SimpleNamespace(
        known=lambda: entries, entries=lambda: entries, core_name=lambda: "Wohnzimmer-Pi"
    )
    spec = core_only_spec(own, registry)  # type: ignore[arg-type]
    schema = spec.input_model.model_json_schema()["properties"]["device"]
    assert "core" in json.dumps(schema)
    payload = spec.input_model.model_validate({"package_id": "VideoLAN.VLC", "device": "Laptop"})
    data = payload.model_dump()
    assert spec.scope is not None
    scope = spec.scope(data)
    assert scope.target == "device:none"
    answer = await spec.handler(data)
    assert answer["error"] == NOT_AVAILABLE_ON_DEVICE and ran == []
    here = spec.input_model.model_validate({"package_id": "VideoLAN.VLC"}).model_dump()
    scope = spec.scope(here)
    assert scope.device_name == "Wohnzimmer-Pi" and scope.target == "VideoLAN.VLC"
    await spec.handler(here)
    assert len(ran) == 1


def test_a_permission_question_names_the_device_in_structured_fields() -> None:
    from nox.security.model import PermissionRequest, Risk

    def asked(**fields: Any) -> Any:
        from nox.security.permissions import DefaultPermissionEngine

        request = PermissionRequest(
            agent="companion", tool="file", action="write", mode="companion", risk=Risk.HIGH,
            target="Laptop · aaaa: path \"C:/Notizen/a.txt\"", **fields,
        )  # fmt: skip
        pending = SimpleNamespace(request=request, created_at=__import__("datetime").datetime.now())
        engine = SimpleNamespace(
            _confirm_timeout_s=60,
            _may_remember=lambda _p: False,
            _session_grant_ttl=None,
            _satellite_of=lambda _c: "",
        )
        return DefaultPermissionEngine._requested(engine, "g", pending)  # type: ignore[arg-type]

    on_node = asked(device=LAPTOP, device_name="Laptop · aaaa")
    assert on_node.device_id == LAPTOP and on_node.device_name == "Laptop · aaaa"
    assert on_node.target.startswith("Laptop · aaaa:")  # the target text stays as it was
    here = asked(device_name="Wohnzimmer-Pi")
    assert here.device_id == "core" and here.device_name == "Wohnzimmer-Pi"
    alone = asked()
    assert alone.device_id == "" and alone.device_name == ""
    assert (
        "device_name"
        not in PermissionRequest(
            agent="a", tool="t", action="x", mode="m", risk=Risk.LOW, device_name="x"
        ).model_dump()
    )


# ---- M3: the device's dialog ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "de", "en"),
    [
        ("screen.capture", {}, "Bildschirmfoto", "screenshot"),
        ("camera.snapshot", {}, "Kamera", "camera"),
        ("computer.session_start", {"purpose": "Ordnen", "minutes": 7}, "bis 7 min", "up to 7 min"),
        ("shell.run", {"argv": ["ls"]}, "Befehl", "command"),
    ],
)
def test_the_dialog_opens_with_one_plain_sentence(
    tool: str, args: dict[str, Any], de: str, en: str
) -> None:
    from nox.node.agent import confirmation_text

    german = confirmation_text(tool, args, lang="de")
    english = confirmation_text(tool, args, lang="en")
    assert german is not None and english is not None
    assert de in german.splitlines()[0] and en in english.splitlines()[0]
    assert tool in german and german.endswith("Ablehnen)") and english.endswith("deny)")


def test_the_session_dialog_says_what_a_session_means() -> None:
    from nox.node.agent import confirmation_text

    text = confirmation_text("computer.session_start", {"purpose": "Fenster ordnen"}, lang="de")
    assert text is not None and text.startswith(
        "Nox steuert bis 15 min Maus und Tastatur und kann in dieser Zeit alles, was du an "
        "diesem PC kannst."
    )


# ---- K1: protocol extensibility ------------------------------------------------------------------


def test_messages_of_the_next_protocol_with_new_fields_are_accepted() -> None:
    hello = NodeHello.model_validate(
        {
            "protocolVersion": "2025-06-18",
            "node_protocol": 1,
            "node_version": "0.9.0",
            "os": "linux",
            "arch": "x86_64",
            "manifest_hash": "0" * 64,
            "features": ["voice", "something_new"],
            "a_field_of_protocol_2": {"x": 1},
        }
    )
    assert hello.features == ["voice", "something_new"]
    pong = Ping.model_validate(
        {"seq": 1, "ts": 1.0, "session": {"gen": 1, "active": False, "n": 2}, "z": 1}
    )  # noqa: E501
    assert pong.session is not None and pong.session.gen == 1
    with pytest.raises(ValueError):
        Ping.model_validate({"seq": "one", "ts": 1.0})  # known fields stay strict


async def test_a_node_of_another_protocol_hears_compatible_false(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from tests.unit.node.conftest import client_context, hello, open_node, pair_node, recv_until

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    newer = hello(2)
    newer["params"]["os"] = "plan9"  # not something this release knows
    newer["params"]["capabilities_v2"] = ["x"]
    await ws.send(json.dumps(newer))
    answer = await recv_until(ws, lambda m: m.get("id") == 1)
    assert answer["result"]["compatible"] is False
    assert answer["result"]["node_protocol"] == [1, 1]
    assert answer["result"]["node_id"] == node.node_id


async def test_a_hello_of_this_protocol_with_unknown_fields_connects(
    listener: Any, pairing: Any, tls: Any, tmp_path: Path
) -> None:
    from tests.unit.node.conftest import client_context, hello, open_node, pair_node, recv_until

    node = await pair_node(pairing, tmp_path)
    ws = await open_node(listener, client_context(tls, node))
    message = hello(1)
    message["params"]["later_field"] = True
    await ws.send(json.dumps(message))
    answer = await recv_until(ws, lambda m: m.get("id") == 1)
    assert answer["result"]["compatible"] is True
    await ws.send(
        json.dumps(
            {"jsonrpc": "2.0", "method": "nox/pong", "params": {"seq": 1, "ts": 1.0, "new": 1}}
        )
    )  # noqa: E501
    await asyncio.sleep(0.1)
    assert listener.sessions[node.node_id].violations == 0


# ---- M2 (architecture): one writer for node.link -------------------------------------------------


async def test_set_linked_writes_the_database_and_memory_together() -> None:
    from nox.node.registry import NodeEntry, NodeRegistry

    written: list[tuple[str, str | None]] = []
    nodes = SimpleNamespace(set_linked_device=lambda n, d: written.append((n, d)))
    registry = NodeRegistry(SimpleNamespace(), nodes)  # type: ignore[arg-type]
    registry._entries[LAPTOP] = NodeEntry(device_id=LAPTOP, name="Laptop")
    await registry.set_linked(LAPTOP, BROWSER)
    assert written == [(LAPTOP, BROWSER)] and registry.known()[0].linked_device_id == BROWSER


def test_a_device_reports_its_folders_for_the_devices_page() -> None:
    from nox.node.registry import reported_folders

    listed = {"_nox": {"folders": ["C:/Nox-Ablage", 5, "D:/Fotos\x1b[2J"]}}
    assert reported_folders(listed) == ("C:/Nox-Ablage", "D:/Fotos\\x1b[2J")
    assert reported_folders({"_nox": {"folders": "C:/"}}) == ()


# ---- H2: the node's terminal during a session ----------------------------------------------------


def _home(tmp_path: Path) -> Any:
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "node")
    home.ensure()
    return home


def _session(home: Any, *, until: float | None = None, raw: str | None = None) -> None:
    from nox.node.world import SESSION_MARKER

    marker = home.path / SESSION_MARKER
    if raw is not None:
        marker.write_text(raw, encoding="utf-8")
        return
    marker.write_text(json.dumps({"until": until or time.time() + 600}), encoding="utf-8")


@pytest.mark.parametrize("raw", [None, "not json", '{"until": "soon"}', "[]"])
def test_widening_commands_refuse_while_a_session_runs_or_cannot_be_read(
    tmp_path: Path, raw: str | None
) -> None:
    from nox.node import cli, satellite_cli
    from nox.node.voice import MUTE_FILE

    home = _home(tmp_path)
    _session(home, raw=raw)
    asked: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    with pytest.raises(cli.NodeCliError, match="mouse and keyboard"):
        # `notify` works on every OS: the session refusal is what is tested, not the platform.
        cli.allow(home, "notify", True, say=print, interactive=lambda: True, confirm=confirm)
    with pytest.raises(cli.NodeCliError):
        cli.games(home, "Game.exe", False, say=print, interactive=lambda: True, confirm=confirm)
    with pytest.raises(cli.NodeCliError):
        cli.resume(home)
    (home.path / MUTE_FILE).write_text("muted", encoding="utf-8")
    with pytest.raises(satellite_cli.SatelliteCliError):
        satellite_cli.voice_mute(home, False)
    assert (home.path / MUTE_FILE).exists()
    with pytest.raises(satellite_cli.SatelliteCliError):
        satellite_cli.obs_resume(home)
    assert asked == []  # refused before anything is asked
    # Narrowing always works.
    assert "Microphone muted" in satellite_cli.voice_mute(home, True)
    cli.allow(home, "media", False, say=print)


def test_shell_is_not_available_on_a_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 1: `allow shell` on macOS says so plainly, whatever else is going on."""
    from nox.node import cli, cli_text

    real = cli_text.unsupported_reason
    monkeypatch.setattr(cli, "unsupported_reason", lambda group: real(group, "darwin"))
    home = _home(tmp_path)
    with pytest.raises(cli.NodeCliError, match="not available on this device"):
        cli.allow(home, "shell", True, say=print, interactive=lambda: True, confirm=lambda _p: True)


def test_update_refuses_to_install_during_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import cli

    home = _home(tmp_path)
    _session(home)
    check = SimpleNamespace(status="available", message="Nox 0.9.0 is available", version="0.9.0")
    applied: list[Any] = []
    updater = SimpleNamespace(
        check=lambda retry_bad: check, stale_days=lambda: None, apply=applied.append
    )
    monkeypatch.setattr(cli, "node_updater", lambda _home: updater)
    said: list[str] = []
    assert cli.update(home, check_only=False, yes=True, say=said.append, ask=lambda _q: True) == 1
    assert applied == [] and "mouse and keyboard" in said[-1]


def test_a_marker_whose_time_is_over_refuses_nothing(tmp_path: Path) -> None:
    from nox.node import cli
    from nox.node.world import clear_session_marker, session_marker_state

    home = _home(tmp_path)
    _session(home, until=time.time() - 1)
    assert session_marker_state(home.path) is None and cli.session_refusal(home) is None
    _session(home)
    assert session_marker_state(home.path) == "running"
    clear_session_marker(home.path)
    assert session_marker_state(home.path) is None


def test_the_world_keeps_the_marker_while_a_session_runs(tmp_path: Path) -> None:
    from nox.node.world import SESSION_MARKER, World, session_marker_state

    async def notice(_text: str) -> None:
        return None

    world = World(
        notice=notice,
        computer_enabled=lambda: True,
        dialog_open=lambda: False,
        browser_dir=tmp_path / "browser",
        marker=tmp_path / SESSION_MARKER,
    )
    world.session_changed(True, minutes=5)
    assert session_marker_state(tmp_path) == "running"
    world.session_changed(False)
    assert session_marker_state(tmp_path) is None


class _Proc:
    def __init__(self, pid: int, name: str, cmdline: list[str], children: list[Any] | None = None):
        self.pid, self._name, self._cmdline = pid, name, cmdline
        self._children = children or []
        self._parent: Any = None

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def children(self, recursive: bool = False) -> list[Any]:
        if not recursive:
            return list(self._children)
        found: list[Any] = []
        for child in self._children:
            found += [child, *child.children(recursive=True)]
        return found

    def parent(self) -> Any:
        return self._parent


def test_a_terminal_running_nox_node_counts_as_a_nox_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    from nox.desktop.computer_use import terminal_runs_nox

    node_cli = _Proc(30, "python.exe", ["python.exe", "-I", "start_node.pyw", "--cli", "node"])
    shell = _Proc(20, "pwsh.exe", ["pwsh.exe"], [node_cli])
    terminal = _Proc(10, "WindowsTerminal.exe", ["WindowsTerminal.exe"], [shell])
    editor = _Proc(40, "notepad.exe", ["notepad.exe"])
    other_shell = _Proc(50, "cmd.exe", ["cmd.exe"], [_Proc(51, "ping.exe", ["ping"])])
    secret = _Proc(60, "conhost.exe", ["conhost.exe"])
    procs = {p.pid: p for p in (node_cli, shell, terminal, editor, other_shell, secret)}

    def process(pid: int) -> Any:
        if pid == 60:
            return _Unreadable(pid)
        if pid not in procs:
            raise psutil.NoSuchProcess(pid)
        return procs[pid]

    class _Unreadable(_Proc):
        def __init__(self, pid: int) -> None:
            super().__init__(pid, "conhost.exe", [])

        def children(self, recursive: bool = False) -> list[Any]:
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(psutil, "Process", process)

    def never(_pid: int) -> bool:
        return False

    assert terminal_runs_nox(10, is_nox=never)  # Windows Terminal with nox-node beneath it
    assert terminal_runs_nox(20, is_nox=never)
    assert not terminal_runs_nox(40, is_nox=never)  # no terminal
    assert not terminal_runs_nox(50, is_nox=never)  # a terminal without Nox
    assert terminal_runs_nox(60, is_nox=never)  # a tree that cannot be read: fail closed
    assert not terminal_runs_nox(99, is_nox=never)  # gone


def test_computer_use_never_clicks_into_a_terminal_running_nox() -> None:
    from nox.desktop.computer_use import ComputerUse

    use = ComputerUse(
        enabled=lambda: True,
        game_running=lambda: False,
        game_in_foreground=lambda: False,
        nox_pids=set,
        sender=lambda: None,  # type: ignore[arg-type,return-value]
        audit=lambda *_a: None,
        nox_process=lambda _pid: False,
        terminal_nox=lambda pid: pid == 10,
    )
    assert use._is_nox((10, "Windows PowerShell"))
    assert not use._is_nox((11, "Windows PowerShell"))
    broken = ComputerUse(
        enabled=lambda: True,
        game_running=lambda: False,
        game_in_foreground=lambda: False,
        nox_pids=set,
        sender=lambda: None,  # type: ignore[arg-type,return-value]
        audit=lambda *_a: None,
        nox_process=lambda _pid: False,
        terminal_nox=lambda _pid: 1 / 0 > 0,
    )
    assert broken._is_nox((12, "x"))  # cannot tell: Nox's


# ---- UX: the terminal's words --------------------------------------------------------------------


def test_allowing_a_folder_switches_files_on_in_one_question(tmp_path: Path) -> None:
    from nox.node import cli
    from nox.node.manifest import load_manifest

    home = _home(tmp_path)
    cli.confirm_defaults(home, None)
    folder = tmp_path / "Ablage"
    folder.mkdir()
    asked: list[str] = []
    said: list[str] = []

    def confirm(prompt: str) -> bool:
        asked.append(prompt)
        return True

    cli.allow(
        home,
        "files",
        True,
        say=said.append,
        interactive=lambda: True,
        confirm=confirm,
        folder=str(folder),
    )  # noqa: E501
    manifest = load_manifest(home.manifest_file)
    assert manifest.groups["files"] and manifest.folders == [str(folder.resolve())]
    assert asked == [f"Nox may then read and write files in {folder.resolve()}. Allow?"]


def test_allowing_files_without_a_folder_says_so(tmp_path: Path) -> None:
    from nox.node import cli

    home = _home(tmp_path)
    cli.confirm_defaults(home, None)
    said: list[str] = []
    cli.allow(
        home, "files", True, say=said.append, interactive=lambda: True, confirm=lambda _p: True
    )
    assert "no folder allowed yet" in said[-1] and "allow files --folder" in said[-1]


@pytest.mark.parametrize(
    ("group", "platform", "reason"),
    [
        ("packages", "win32", "not_yet"),
        ("desktop", "linux", "not_here"),
        ("computer", "darwin", "not_here"),
        ("shell", "darwin", "not_here"),
        ("media", "darwin", "not_here"),
        ("media", "linux", None),
        ("files", "darwin", None),
    ],
)
def test_what_exists_on_which_device(group: str, platform: str, reason: str | None) -> None:
    from nox.node.cli_text import unsupported_reason

    assert unsupported_reason(group, platform) == reason


def test_allow_refuses_a_group_that_cannot_work_here(tmp_path: Path) -> None:
    from nox.node import cli

    home = _home(tmp_path)
    with pytest.raises(cli.NodeCliError, match="not available on devices yet"):
        cli.allow(
            home, "packages", True, say=print, interactive=lambda: True, confirm=lambda _p: True
        )


def test_yes_and_no_in_both_languages() -> None:
    from nox.node.cli_text import ask_yes

    prompts: list[str] = []

    def reader(answer: str) -> Any:
        def read(prompt: str) -> str:
            prompts.append(prompt)
            return answer

        return read

    assert ask_yes("Erlauben?", lang="de", read=reader("j"))
    assert ask_yes("Allow?", lang="en", read=reader("ja"))
    assert ask_yes("Allow?", lang="en", read=reader("Y"))
    assert not ask_yes("Erlauben?", lang="de", read=reader(""))
    assert not ask_yes("Erlauben?", lang="de", read=reader("n"))
    assert prompts[0].endswith("[j/N] ") and prompts[1].endswith("[y/N] ")


def test_the_command_is_named_as_the_device_needs_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node import cli_text

    monkeypatch.delenv("NOX_NODE_HOME", raising=False)
    monkeypatch.setattr(cli_text.getpass, "getuser", lambda: "alex")
    assert cli_text.node_command("win32") == "nox-node"
    assert cli_text.node_command("linux") == "nox-node"
    monkeypatch.setenv("NOX_NODE_HOME", "/var/lib/nox-node/node")
    assert cli_text.node_command("linux") == "sudo nox-node"
    assert cli_text.node_command("win32") == "nox-node"


def test_german_words_and_labels_match_the_devices_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node.cli_text import group_label, language, t

    monkeypatch.setenv("NOX_LANG", "de")
    assert language() == "de"
    assert group_label("files") == "Dateien lesen und schreiben"
    assert group_label("camera") == "Kamera nutzen"
    assert group_label("voice") == "Zuhören und sprechen"
    assert t("allow_ask_folder", folder="D:/Ablage") == (
        "Nox darf dann Dateien in D:/Ablage lesen und schreiben. Erlauben?"
    )
    assert t("allow_files_no_folder", add="x").startswith("Dateien: an, aber noch kein Ordner")
    monkeypatch.setenv("NOX_LANG", "en")
    assert group_label("files") == "read and write files"


# ---- N1: pairing again ---------------------------------------------------------------------------


def test_pair_again_only_when_the_pairing_cannot_work(tmp_path: Path) -> None:
    from nox.node import cli

    home = _home(tmp_path)
    link = "nox-node://pair?h=192.168.1.20&p=47843&c=ABCDEFGH&fp=" + "ab" * 32
    with pytest.raises(cli.NodeCliError, match="not paired"):
        cli.pair(link, home=home, again=True)
    identity = SimpleNamespace()
    home.load_identity = lambda: identity  # type: ignore[method-assign]
    home.pinned_ca_ok = lambda _i: True  # type: ignore[method-assign]
    home.read_status = lambda: {"state": "refused"}  # type: ignore[method-assign]
    assert cli.again_reason(home) == "refused"
    home.read_status = lambda: {"state": "connected"}  # type: ignore[method-assign]
    assert cli.again_reason(home) == "certificate_unreadable"  # no certificate file
    home.pinned_ca_ok = lambda _i: False  # type: ignore[method-assign]
    assert cli.again_reason(home) == "pin_mismatch"


def test_render_node_names_the_real_command() -> None:
    from nox.lan.terminal import render_node

    shown = render_node(
        {
            "link": "nox-node://pair?h=192.168.1.20",
            "code": "ABCDEFGH",
            "expires_at": "2026-10-24T12:00:00+00:00",
            "ca_fingerprint": "AB:" * 10,
        },
        language="en",
    )
    assert 'nox-node pair "nox-node://pair?h=192.168.1.20"' in shown and "sudo nox-node" in shown
    assert "nox node pair" not in shown
