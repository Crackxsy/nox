"""Shared helpers for the tests around a computer-use session on a node: a scripted device
(screen, camera, windows, games), the gate in front of it, the core's routed tools, a
locked `NodeControl`, and the PIN checks and requests of the dashboard and paired devices."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from nox.desktop.computer_use import DryRunSender
from nox.desktop.win32 import WindowInfo
from nox.node.device.gate import NodeGate
from nox.node.device.home import NodeHome
from nox.node.device.world import GameLock, PlatformMedia, World, world_tools
from nox.node.hub.registry import ROUTABLE
from nox.node.hub.world_routes import NodeActions, NodeControl, NodeLookups, world_specs
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier
from nox.security.gate import PinRequiredError
from tests.unit.node.support import FakeRegistry, registry_entry, signed

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 100


LAPTOP = "a" * 32


BROWSER = "f" * 32


SENSE_GROUPS = ("screen", "camera", "browser", "desktop", "computer", "media")


class Probe:
    """A scripted window list of the device: one editor window, focus requests recorded."""

    def __init__(self) -> None:
        self.focused: list[int] = []

    def windows(self) -> list[WindowInfo]:
        return [WindowInfo(7, "Notizen - Editor", 4242, "notepad.exe", False)]

    def focus(self, handle: int) -> bool:
        self.focused.append(handle)
        return True

    minimize = restore = close = focus


def device_world(
    *,
    windows: bool = True,
    game: str = "",
    front: str = "",
    notice_fails: bool = False,
    computer_on: bool = True,
) -> tuple[World, dict[str, Any]]:
    """A scripted device (screen, camera, windows, a game in front or not) and what it saw."""
    seen: dict[str, Any] = {"notices": [], "sender": DryRunSender(), "grabs": 0}

    async def notice(text: str) -> None:
        if notice_fails:
            raise RuntimeError("no notification service")
        seen["notices"].append(text)

    async def grab() -> bytes:
        seen["grabs"] += 1
        return PNG

    lock = GameLock(
        watched=lambda: ["RocketLeague.exe"],
        running_names=lambda: {game.lower()} if game else set(),
        foreground_name=lambda: front or "explorer.exe",
        process_pids=lambda _names: {999} if game else set(),
        services=set,
        screen_state=lambda: 5,  # QUNS_ACCEPTS_NOTIFICATIONS: nothing owns the screen
        windows=windows,
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
        has_module=lambda _module: True,  # every optional library "installed"
        media=PlatformMedia(platform="linux"),  # the same catalogue on every test OS
    )
    return world, seen


def world_gate(world: World, keys: Path) -> tuple[NodeGate, ReceiptSigner]:
    """The node's gate in front of `world`, with the senses and input groups switched on."""
    signer = ReceiptSigner(keys / "receipts")
    manifest = NodeManifest(groups={g: g in SENSE_GROUPS or g == "system" for g in GROUPS})
    gate = NodeGate(world_tools(world), manifest, None)
    gate.input_lock = world.lock.reason
    gate.receipts = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    return gate, signer


def world_meta(signer: ReceiptSigner, name: str, args: dict[str, Any], risk: str) -> NoxCallMeta:
    """Validated call metadata with a receipt the gate of `world_gate` accepts."""
    meta = signed(signer, name, args, risk=risk, device_id="d" * 32, core_id="c" * 64)
    return NoxCallMeta.model_validate(meta)


def privacy_state(mode: str = "balanced", camera: bool = False, screen: bool = True) -> Any:
    """The core's privacy state as the capture tools read it."""
    from nox.core.state import PrivacyMode

    return SimpleNamespace(
        mode=PrivacyMode(mode),
        active_zone=None,
        allows_capture=lambda kind: {"camera": camera, "screen": screen}.get(kind, False),
        allows_cloud=lambda: mode in ("full", "balanced"),
        allows_screenshot_to_cloud=lambda: mode == "full",
    )


def routed_specs(
    tmp_path: Path,
    privacy: Any,
    answer: Callable[..., Awaitable[dict[str, Any]]],
    *,
    linked: str | None = BROWSER,
) -> tuple[dict[str, Any], FakeRegistry, NodeControl, list[Any]]:
    """The core's routed tools for one laptop whose answers come from `answer`, with the
    registry, the session control and the grants they produce."""
    laptop = registry_entry(
        LAPTOP,
        "Laptop",
        tools={n: "high" for n in ROUTABLE if n.startswith(("screen", "camera", "computer"))},
        linked=linked,
    )
    laptop.confirmer = True
    registry = FakeRegistry([laptop])
    registry.call = answer  # type: ignore[method-assign]
    granted: list[Any] = []
    engine = SimpleNamespace(grant_temporary=granted.append, revoke=lambda gid: granted.append(gid))
    notices: list[dict[str, Any]] = []
    ends: list[str] = []

    async def end_on_node(entry: Any) -> dict[str, Any]:
        ends.append(entry.device_id)
        return await answer(entry, "computer.session_end", {})

    control = NodeControl(engine, notices.append, actions=NodeActions(end_on_node=end_on_node))
    specs = world_specs(
        registry=registry,  # type: ignore[arg-type]
        own=lambda _n: None,
        defaults=dict,
        control=control,
        captures_root=lambda: tmp_path / "captures",
        privacy=lambda: privacy,
    )
    return {s.name: s for s in specs}, registry, control, [granted, notices, ends]


# ---- computer use on the core's side -------------------------------------------------------------


def session_report(gen: int, active: bool, sid: str = "") -> dict[str, Any]:
    """A node's session report: generation, active or not, session id."""
    return {"gen": gen, "active": active, "id": sid or ("ab" * 16 if active else "")}


async def route_call(specs: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a routed tool on the laptop the way the executor does: scope first, then handler."""
    spec = specs[name]
    payload = spec.input_model.model_validate({**args, "device": "Laptop"}).model_dump()
    spec.scope(payload)
    result: dict[str, Any] = await spec.handler(payload)
    return result


# ---- approvals and changes while the lock holds --------------------------------------------------

OTHER_PHONE = "e" * 32


class PinGate:
    """The security PIN on the core: one PIN, required or not."""

    def __init__(self, *, required: bool = True, pin: str = "4711") -> None:
        self.required, self.pin = required, pin

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        if pin != self.pin:
            raise PinRequiredError(action, f"{action} requires the security PIN")


class DeviceGuard:
    """The paired-device PIN check (`DevicePinGuard`): the same PIN, its own lockout."""

    def __init__(self, gate: PinGate) -> None:
        self.gate, self.wrong = gate, 0

    async def require(self, client: Any, pin: str | None, action: str) -> None:
        from nox.ipc.errors import IpcError

        try:
            await self.gate.require(pin, action=action, by=client.client_id)
        except PinRequiredError as exc:
            self.wrong += 1
            raise IpcError("permission.denied", exc.reason) from exc


def locked_control() -> NodeControl:
    """A `NodeControl` that holds the lock for a session on the laptop."""
    control = NodeControl(
        None, lambda _n: None, NodeLookups(linked=lambda d: BROWSER if d == LAPTOP else None)
    )
    control.answered(LAPTOP, session_report(2, True))
    return control


def request_ctx(role: str, client: str, peer: str = "") -> Any:
    """A request context of the given role and client, connecting from `peer`."""
    from nox.ipc.dispatch import RequestContext

    envelope = request_envelope("test.request", role, client, {})
    return RequestContext(client_id=client, role=role, request=envelope, peer=peer)


async def session_refusal(
    control: NodeControl,
    ctx: Any,
    pin: str | None,
    *,
    action: str,
    gate: Any,
    device_guard: Any,
    remote: Callable[[], bool | None],
) -> Any:
    """What the node-session condition says about `action` from `ctx`: the refusal, or None."""
    from nox.node.hub.session_access import NodeSessionCondition
    from nox.security.access import Allow, Call, PinChecks

    pins = PinChecks(gate=lambda: gate)
    pins.device_guard = device_guard
    verdict = await NodeSessionCondition(control, pins, remote).check(ctx.client, Call(action, pin))
    return None if isinstance(verdict, Allow) else verdict


def wired_core(
    gate: PinGate, remote: Callable[[], bool | None] = lambda: False
) -> tuple[Any, NodeControl, DeviceGuard, list[str]]:
    """A core wired exactly like `nox.node.hub.install` (`wire_guards`), with fakes behind it."""
    from nox.ipc.dispatch import RequestRegistry
    from nox.node.hub.install.control import wire_guards

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
    )
    core.registry.policy.pins.gate = lambda: gate
    core.registry.policy.pins.device_guard = device_guard
    control = locked_control()
    wire_guards(core, control, remote=remote)
    return core, control, device_guard, audit


def request_envelope(name: str, role: str, client: str, payload: dict[str, Any]) -> Any:
    """A request envelope from the given role and client."""
    from nox.ipc.protocol import Envelope, Kind, Source

    return Envelope(kind=Kind.REQUEST, name=name, src=Source(role=role, id=client), payload=payload)  # type: ignore[arg-type]


LOCAL, PHONE, LINKED = "dashboard:1", "device:" + OTHER_PHONE, "device:" + BROWSER


class Mute(BaseModel):
    """The payload of the microphone request."""

    muted: bool


class Text(BaseModel):
    """The payload of a chat message."""

    text: str = "hallo"


def conversation_registry() -> tuple[Any, NodeControl, list[str]]:
    """A wired core with the conversation requests registered; returns it with the session
    control and the list of handlers that really ran."""
    core, control, _guard, _audit = wired_core(PinGate())
    ran: list[str] = []

    def handler(name: str) -> Any:
        async def run(_ctx: Any, _p: Any) -> dict[str, Any]:
            ran.append(name)
            if name == "chat.send":  # the answer's fixed shape (`protocol.ChatSendResult`)
                return {"request_id": "r", "text": "ok", "provider": "x", "degraded": False}
            return {}

        return run

    roles = ("dashboard", "shell", "device")
    core.registry.register("chat.send", Text, handler("chat.send"), roles=roles)
    core.registry.register("voice.ptt", Text, handler("voice.ptt"), roles=roles)
    core.registry.register("voice.mute", Mute, handler("voice.mute"), roles=roles)
    core.registry.register("plans.approve", Text, handler("plans.approve"), roles=roles)
    return core, control, ran


# ---- a request as the dispatcher sees it ---------------------------------------------------------


async def send_from(core: Any, name: str, client: str, payload: dict[str, Any], peer: str) -> Any:
    """Dispatch a request as `client` connecting from the address `peer`."""
    from nox.ipc.dispatch import RequestContext

    role = "device" if client.startswith("device:") else "dashboard"
    envelope = request_envelope(name, role, client, payload)
    ctx = RequestContext(client_id=client, role=role, request=envelope, peer=peer)
    return await core.registry.dispatch(ctx, envelope)


OTHER_NODE = "c" * 32


# ---- a node folder without a pairing -------------------------------------------------------------


def plain_node_home(tmp_path: Path) -> Any:
    """An empty node folder: nothing paired, nothing confirmed."""
    home = NodeHome(tmp_path / "node")
    home.ensure()
    return home
