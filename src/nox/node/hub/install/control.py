"""Computer use on nodes, wired onto the core: the lock, the guards, the requests, the watcher.

Owns what makes the approval lock real: `NodeControl` built from the registry and the listener,
the condition that puts it in front of the core's own access policy (`wire_guards`), the two
requests that show and lift it (`node.computer.*`), the hook that vets every new connection, and
the one-second watcher that keeps the lock, the games and the satellites' gates in step with what
the nodes report. Does not own the lock's rules (`nox.node.hub.world_routes`).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.core.config import NoxConfig
from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_UNAVAILABLE, IpcError
from nox.ipc.roles import LOCAL_UI_ROLES, UI_ROLES
from nox.node.hub.console import console_is_remote
from nox.node.hub.install.games import resend_games_if_changed, send_games, watched_games
from nox.node.hub.install.pairing_requests import EmptyRequest
from nox.node.hub.listener import NodeListener, NodeSession
from nox.node.hub.ports import CoreServices
from nox.node.hub.registry import NodeEntry, NodeRegistry
from nox.node.hub.satellites import SatelliteHub
from nox.node.hub.session_access import NodeSessionCondition
from nox.node.hub.world_routes import NodeActions, NodeControl, NodeLookups, world_specs
from nox.node.wire.addresses import all_local_addresses
from nox.security._events import publish_nowait
from nox.security.access import Allow, ClientKind
from nox.tools.registry import ToolSpec

log = get_logger(__name__)

__all__ = [
    "ReleaseRequest",
    "attach_control",
    "build_world",
    "connection_hook",
    "watch_sessions",
    "wire_guards",
]

#: How often the watcher reads the nodes' latest reports.
WATCH_INTERVAL_S = 1.0


class ReleaseRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)


def connection_hook(
    config: NoxConfig, registry: NodeRegistry, control: NodeControl, listener: NodeListener
) -> Callable[[NodeSession], Awaitable[None]]:
    """What happens on every new authenticated node connection: its hello resets how computer
    use stands there (`NodeControl.hello`), the registry lists its tools, and it gets the core's
    watched games. A hello that claims a computer-use session is a protocol violation: the
    connection ends before anything else (no session of a new connection was started here)."""

    async def on_connected(session: NodeSession) -> None:
        if not control.hello(session.device_id, session.computer):
            await listener.drop(session.device_id, "session_in_hello")
            return
        await registry.on_connected(session)
        await send_games(config, session)

    return on_connected


async def watch_sessions(
    control: NodeControl,
    config: NoxConfig,
    listener: NodeListener,
    satellites: SatelliteHub,
) -> None:
    """Every second: the nodes' latest reports, so the lock, the grant and the dashboard follow
    a session that ends on the node (time, stop hotkey, kill) without another call; the core's
    watched games, sent again when they change; and the satellites' microphone gate and overlay
    credentials, which follow the privacy settings and the Devices page."""
    sent: list[list[str]] = [watched_games(config)]  # sent on every connection already
    while True:
        await asyncio.sleep(WATCH_INTERVAL_S)
        try:
            control.active()
            await resend_games_if_changed(config, listener, sent)
            await satellites.push_gates()
            await satellites.sweep()
        except Exception as exc:  # noqa: BLE001 - the watcher outlives one bad round
            log.error("node.computer_watch_failed", error=type(exc).__name__)


def attach_control(satellites: SatelliteHub, control: NodeControl) -> None:
    """Computer use and the satellites under the computer-use lock: the node Nox controls
    is refused (`controlled_here`) - and every node while a binding is saturated, when nobody
    can be told apart from the controlled PC any more; while any session runs, voice turns from
    other nodes are marked as holding unreadable untrusted content, like paired-device chat."""

    def controlled(device_id: str) -> bool:
        return bool(control.locked(device_id) or control.is_saturated())

    satellites.bind_control(controlled=controlled, session_active=control.active)


def build_world(
    services: CoreServices,
    registry: NodeRegistry,
    defaults: Callable[[], Any],
    listener: NodeListener | None,
) -> tuple[list[ToolSpec], NodeControl]:
    """Captures, browser, desktop, computer use and media, routable to nodes; and the
    record of computer-use sessions on nodes (dashboard notice, grant, "allow" lock).
    `listener` is None where there is none: a harness with simulated devices."""
    security = services.security
    config = services.config

    def publish(payload: dict[str, Any]) -> None:
        publish_nowait(services.bus, "desktop.computer_use", payload, source="node")

    async def end_on_node(entry: NodeEntry) -> dict[str, Any]:
        answer: dict[str, Any] = await registry.call(
            entry, "computer.session_end", {}, risk="low", timeout_s=10
        )
        return answer

    def offers_computer(device_id: str, session: NodeSession) -> bool:
        entry = next((e for e in registry.known() if e.device_id == device_id), None)
        return (
            entry is not None
            and entry.session is session
            and "computer.session_start" in entry.tools
        )

    def reports() -> list[tuple[str, Any]]:
        # "Open" is read only from a node that offers computer use on this very connection;
        # "over" from any node (it only ever lifts).
        if listener is None:
            return []
        return [
            (d, s.computer)
            for d, s in listener.sessions.items()
            if s.computer is not None and (not s.computer.active or offers_computer(d, s))
        ]

    def addresses() -> dict[str, tuple[str, list[str]]]:
        if listener is None:
            return {}
        return {d: (s.peer_host, list(s.reported_addresses)) for d, s in listener.sessions.items()}

    def audit_control(what: str) -> None:
        security.audit.append(
            actor="core",
            tool="node",
            action="node.computer.binding",
            target="",
            decision="deny",
            result=what[:64],
        )

    def linked(device_id: str) -> str | None:
        entry = next((e for e in registry.known() if e.device_id == device_id), None)
        return entry.linked_device_id if entry is not None else None

    def label_of(device_id: str) -> str:
        entry = next((e for e in registry.entries() if e.device_id == device_id), None)
        return entry.label if entry is not None else device_id[:8]

    pending: set[asyncio.Task[Any]] = set()

    def violation(device_id: str, why: str) -> None:
        # A computer-use session this core never started: the connection ends (audited there).
        if listener is None:
            return
        with contextlib.suppress(RuntimeError):  # no loop: nothing is connected either
            task = asyncio.get_running_loop().create_task(listener.drop(device_id, why))
            pending.add(task)
            task.add_done_callback(pending.discard)

    control = NodeControl(
        security.engine,
        publish,
        NodeLookups(
            reports=reports,
            labels=label_of,
            linked=linked,
            addresses=addresses,
            own_addresses=all_local_addresses,
        ),
        NodeActions(end_on_node=end_on_node, on_violation=violation, audit=audit_control),
        stop_hotkey=config.desktop.computer_use.stop_hotkey,
    )
    # While a session runs, high risk is left to no other node's dialog.
    registry.may_delegate = control.may_delegate
    wire_guards(services, control)
    # A stop never lifts the lock by itself: the node ends its session on `nox/kill` and says
    # so in its next pong, which `pull` reads (also every second, `watch_sessions`).
    captures = Path(config.paths.runtime_dir) / "hands" / "captures"
    specs = world_specs(
        registry=registry,
        own=services.tool_registry.get,
        defaults=defaults,
        control=control,
        captures_root=lambda: captures,
        privacy=lambda: security.privacy,
    )
    return specs, control


def wire_guards(
    services: CoreServices, control: NodeControl, *, remote: Callable[[], bool | None] | None = None
) -> None:
    """While Nox controls a node: every "allow" and every request that changes something goes
    through `NodeSessionCondition`, a condition of the core's access policy; and the
    `node.computer.*` requests. Separate so a test can wire a fake core exactly like this.
    `remote` tells whether this computer is used through a remote session; by default the real
    probe (`nox.node.hub.console.console_is_remote`), looked up when this runs."""
    policy = services.registry.policy
    probe = remote if remote is not None else console_is_remote
    policy.add_condition(NodeSessionCondition(control, policy.pins, probe))
    register_control_requests(services, control)


def register_control_requests(services: CoreServices, control: NodeControl) -> None:
    """`node.computer.status` (what the header shows, for a dashboard that (re)connects) and
    `node.computer.release` (lift a stuck lock by hand: this machine's own shell or dashboard
    with the PIN - also over SSH to a headless core, `nox node release`; a node that still
    reports the session this core started locks again at once)."""
    ipc = services.registry

    async def h_status(ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        # Whether the asking browser is the one on a PC Nox controls: it cannot approve then,
        # so it shows no PIN field at all.
        client = ctx.client
        here = client.kind is ClientKind.DEVICE and (
            control.controls_browser(client.device_id) or control.from_controlled(client.peer)
        )
        return {"sessions": control.status(), "controlled_here": here}

    async def h_release(ctx: RequestContext, p: ReleaseRequest) -> dict[str, Any]:
        verdict = await ipc.policy.pins.local_ui(ctx.client, p.pin, "node.computer.release")
        if not isinstance(verdict, Allow):
            services.security.audit.append(
                actor=ctx.client_id or ctx.role,
                tool="node",
                action="node.computer.release",
                target="",
                decision="deny",
                result=verdict.reason[:64],
            )
            raise IpcError(
                ERR_UNAVAILABLE,
                verdict.reason,
                details={"reason": "refused", "sub_reason": verdict.code},
            )
        released = control.release("released by the owner")
        services.security.audit.append(
            actor=ctx.client_id or ctx.role,
            tool="node",
            action="node.computer.release",
            target="",
            decision="allow",
            result=f"released {released}",
        )
        return {"ok": True, "released": released, "sessions": control.status()}

    ipc.register("node.computer.status", EmptyRequest, h_status, roles=UI_ROLES)
    ipc.register("node.computer.release", ReleaseRequest, h_release, roles=LOCAL_UI_ROLES)
