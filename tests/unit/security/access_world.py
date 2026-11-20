"""A core wired the way production wires it, for the tests that pin down who may do what while
synthetic input or a node session is in play.

`build_world` is the one place a test learns how the two locks are installed; the tests only
use what it returns: `send` for a request through the real dispatcher, `reply` for an answer to a
permission prompt through the real handler, and the three switches `local_lock`, `node` and
`remote` that put the world into one state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import IpcError
from nox.ipc.handlers.core import CoreHandlers, PermissionReply
from nox.ipc.protocol import Envelope, Kind, Source
from nox.node.world_routes import NodeControl
from nox.security.access import InputLock
from nox.security.model import Decision, Risk
from tests.unit.node.support_sessions import (
    BROWSER,
    LAPTOP,
    DeviceGuard,
    PinGate,
    session_report,
)

PIN = "4711"
OTHER_PHONE = "e" * 32

#: The clients of the table: the first two are this machine's own surfaces.
CLIENTS: dict[str, tuple[str, str, str]] = {
    # name: (role, client id, peer address)
    "dashboard": ("dashboard", "dashboard:1", ""),
    "shell": ("shell", "shell:1", ""),
    "phone": ("device", "device:" + OTHER_PHONE, "192.168.1.20"),
    "linked": ("device", "device:" + BROWSER, "192.168.1.30"),
    # Not linked in the dashboard, but connecting from the address of the PC Nox controls.
    "at_the_pc": ("device", "device:" + "d" * 32, "192.168.1.40"),
    "plugin": ("plugin", "plugin:1", ""),
}
#: Where the controlled node connects from: another PC, or this very machine.
OTHER_PC, THIS_PC = "192.168.1.40", "127.0.0.1"


class Anything(BaseModel):
    """Any payload: the handlers of the table only record that they ran."""

    model_config = {"extra": "ignore"}


class Mute(BaseModel):
    muted: bool


class Strict(BaseModel):
    """A request that takes no PIN of its own: a PIN in the payload is a validation error."""

    model_config = {"extra": "forbid"}


@dataclass
class World:
    """One core, its dispatcher and its switches."""

    registry: RequestRegistry
    handlers: CoreHandlers
    control: NodeControl
    replied: list[str]
    addresses: dict[str, tuple[str, list[str]]]
    ran: list[str] = field(default_factory=list)
    locked: list[bool] = field(default_factory=lambda: [False])
    remote: list[bool | None] = field(default_factory=lambda: [False])
    risk: list[Risk | None] = field(default_factory=lambda: [None])

    def local_lock(self, on: bool) -> None:
        """Synthetic input is in play on this machine (computer use)."""
        self.locked[0] = on

    def node(self, state: str) -> None:
        """The node session: none, on another PC, on this very PC, or with a full binding."""
        if state == "off":
            self.control.observe(LAPTOP, session_report(9, False))
            return
        self.addresses[LAPTOP] = (THIS_PC if state == "this_pc" else OTHER_PC, [])
        self.control.answered(LAPTOP, session_report(9, True))
        if state == "saturated":
            self.control.saturated.add(LAPTOP)

    async def send(self, client: str, name: str, payload: dict[str, Any]) -> Envelope:
        role, client_id, peer = CLIENTS[client]
        envelope = Envelope(
            kind=Kind.REQUEST, name=name, src=Source(role=role, id=client_id), payload=payload
        )
        ctx = RequestContext(client_id=client_id, role=role, request=envelope, peer=peer)
        return await self.registry.dispatch(ctx, envelope)

    async def reply(self, client: str, decision: Decision, pin: str | None) -> dict[str, Any]:
        role, client_id, peer = CLIENTS[client]
        envelope = Envelope(
            kind=Kind.REQUEST, name="security.permission.reply", src=Source(role=role, id=client_id)
        )
        ctx = RequestContext(client_id=client_id, role=role, request=envelope, peer=peer)
        answer = PermissionReply(grant_id="g", decision=decision, pin=pin)
        return await self.handlers.permission_reply(ctx, answer)


def build_world(*, pin: PinGate | None = None) -> World:
    """A dispatcher with the requests of the table, the local input lock and the node lock wired
    like `nox.desktop.install` and `nox.node.install.wire_guards`."""
    from nox.node.install import wire_guards

    remote: list[bool | None] = [False]
    gate = pin if pin is not None else PinGate()
    guard = DeviceGuard(gate)
    risk: list[Risk | None] = [None]
    replied: list[str] = []
    core = SimpleNamespace(
        registry=RequestRegistry(),
        security=SimpleNamespace(
            gate=gate,
            engine=SimpleNamespace(
                reply=lambda gid, decision, **_kw: replied.append(decision.value) or True,
                pending_request=lambda _gid: (
                    SimpleNamespace(risk=risk[0]) if risk[0] is not None else None
                ),
            ),
            audit=SimpleNamespace(append=lambda **_kw: None),
        ),
    )
    addresses: dict[str, tuple[str, list[str]]] = {}
    control = NodeControl(
        None,
        lambda _n: None,
        linked=lambda d: BROWSER if d == LAPTOP else None,
        addresses=lambda: addresses,
        own_addresses=lambda: ("192.168.1.10",),
    )
    core.registry.policy.pins.gate = lambda: gate
    core.registry.policy.pins.device_guard = guard
    wire_guards(core, control, remote=lambda: remote[0])
    world = World(  # type: ignore[arg-type]
        core.registry, CoreHandlers(core), control, replied, addresses, remote=remote, risk=risk
    )
    core.registry.policy.add_condition(InputLock(lambda: world.locked[0]))
    ran = world.ran

    def handler(name: str) -> Any:
        async def run(_ctx: Any, _p: Any) -> dict[str, Any]:
            ran.append(name)
            if name == "chat.send":  # the answer's fixed shape (`protocol.ChatSendResult`)
                return {"request_id": "r", "text": "ok", "provider": "x", "degraded": False}
            return {}

        return run

    roles = ("dashboard", "shell", "device", "plugin")
    for name in (
        "state.get",
        "security.kill",
        "plans.list",
        "chat.send",
        "voice.ptt",
        "plans.approve",
        "mcp.servers.add",
        "node.unpair",
        "security.resume",
    ):
        core.registry.register(name, Anything, handler(name), roles=roles)
    core.registry.register("voice.mute", Mute, handler("voice.mute"), roles=roles)
    core.registry.register("strict.change", Strict, handler("strict.change"), roles=roles)
    core.registry.register("strict.list", Strict, handler("strict.list"), roles=roles)
    return world


def outcome(reply: Envelope) -> str:
    """One word for what a request came to: `allow`, `lock` (the local input lock), or the
    stable code the dashboard words itself (`sub_reason`)."""
    if reply.kind is Kind.RESPONSE:
        return "allow"
    payload = reply.payload
    assert payload["code"] == "permission.denied", payload
    details = payload.get("details") or {}
    if not details:
        assert "stop hotkey" in payload["message"], payload
        return "lock"
    assert details["reason"] == "node_session", payload
    return str(details["sub_reason"])


def reply_outcome(answer: dict[str, Any], replied: list[str]) -> str:
    """The same words for an answer to a permission prompt."""
    if answer["ok"]:
        return "allow" if replied[-1:] == ["allow"] else "deny"
    if "sub_reason" in answer:
        return str(answer["sub_reason"])
    assert "stop hotkey" in answer["reason"], answer
    return "lock"


async def reply_error(world: World, client: str, pin: str | None) -> str:
    """What a paired device's high-risk "allow" without the right PIN raises."""
    try:
        await world.reply(client, Decision.ALLOW, pin)
    except IpcError as exc:
        return str(exc.code)
    return "raised nothing"
