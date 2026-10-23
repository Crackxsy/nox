"""`install(core) -> NodeRuntime`: the core's side of `nox node`, wired into a started core.

The requests are registered always, so the dashboard can say honestly why nodes are unavailable;
the node listener (`nox.node.listener`) only starts with network access (`ipc.lan.enabled`),
because it needs the installation's authority and its server certificate.

Requests, for the UI roles (`nox.ipc.roles`):
  * `node.pair.start` - a one-time code and the `nox-node://pair?...` link (and its QR code) that
    carries the authority's fingerprint. Pairing a node gives a PC to Nox's hands, so it needs the
    PIN when one is set - and from a paired device always (`nox.lan.install.authorize_admin`),
  * `node.list` - paired nodes, online or not,
  * `node.unpair` - revoke a node: its certificates go on the denylist and its connection closes
    at once. Narrowing only, so no PIN locally.
  * `node.narrow`, `node.defaults.set` - see `nox.node.policy`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_NOT_FOUND, ERR_UNAVAILABLE, IpcError
from nox.ipc.roles import UI_ROLES
from nox.lan.install import authorize_admin, qr_data_uri
from nox.lan.tls import TlsMaterial
from nox.mcp.client import McpClientError
from nox.node.console import console_is_remote
from nox.node.hands import (
    PathArgs,
    ShellArgs,
    WriteArgs,
    write_risk,
    write_summary,
    write_text_refusal,
)
from nox.node.listener import NodeListener
from nox.node.names import label
from nox.node.pairing import NodePairing
from nox.node.policy import defaults_view, group_of, register_policy_requests
from nox.node.protocol import NodePairRequest, NodePairResponse, PairLink, render
from nox.node.receipts import ReceiptSigner
from nox.node.registry import NodeEntry, NodeRegistry
from nox.node.repo import NodeRepository
from nox.node.router import CannotShow, install_routes, routed_spec
from nox.node.satellites import SatelliteHub, obs_route, voice_say_spec
from nox.node.tools import NoArgs, NotifyArgs, system_info
from nox.node.world_routes import NodeControl, local_pin_refusal, session_refusal, world_specs
from nox.remote.models import DeviceRow
from nox.remote.repo import RemoteRepository
from nox.security.model import Risk
from nox.tools.registry import ToolSpec

log = get_logger(__name__)

__all__ = ["NodeRuntime", "install"]

#: How often the listener compares its certificates with the network listener's.
TLS_RECHECK_S = 60.0
#: How long the node listener waits for the network listener's first certificates.
TLS_WAIT_S = 30.0


class PairStartRequest(BaseModel):
    name: str = Field(default="", max_length=64)
    pin: str | None = Field(default=None, max_length=128)


class UnpairRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    pin: str | None = Field(default=None, max_length=128)


class EmptyRequest(BaseModel):
    pass


class LinkRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    device_id: str | None = Field(default=None, max_length=64)
    pin: str | None = Field(default=None, max_length=128)


#: Redemption reasons -> HTTP status of `POST /api/node/pair`.
_STATUS = {
    "bad_csr": 400,
    "invalid_characters": 400,
    "name_taken": 409,
    "max_devices": 409,
    "issue_failed": 503,
}


@dataclass
class NodeRuntime:
    pairing: NodePairing
    enabled: bool
    listener: NodeListener | None = None
    registry: NodeRegistry | None = None
    #: The satellites' side (C9): voice turns, overlay credentials, the microphone gate.
    satellites: SatelliteHub | None = None
    error: str = ""
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def running(self) -> bool:
        return self.listener is not None and self.listener.running

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.satellites is not None:
            await self.satellites.close()
        if self.registry is not None:
            await self.registry.close()
        if self.listener is not None:
            await self.listener.stop()


def install(core: Any) -> NodeRuntime:
    cfg = core.config.ipc.lan
    lan = getattr(core, "lan_runtime", None)
    tls_dir = Path(core.config.paths.data_dir) / "tls"
    pairing = NodePairing(
        RemoteRepository(core.db),
        NodeRepository(core.db),
        tls_dir=tls_dir,
        bus=core.bus,
        audit=core.security.audit,
        ttl_s=cfg.pair_code_ttl_s,
        max_nodes=cfg.max_nodes,
    )
    runtime = NodeRuntime(pairing=pairing, enabled=bool(cfg.enabled) and lan is not None)

    async def forget_grants(device_id: str, reason: str) -> None:
        # A removed node keeps no remembered "allow" that a re-paired one could inherit.
        engine = getattr(core.security, "engine", None)
        if engine is not None:
            engine.revoke_device_grants(device_id, reason=f"node removed: {reason}"[:64])

    pairing.on_revoked(forget_grants)

    _register_requests(core, runtime, lan)
    if not runtime.enabled or lan is None:
        return runtime

    # The core signs every call with a side effect; nodes pin the public half at pairing.
    signer = ReceiptSigner(tls_dir)
    holder: list[NodeListener] = []
    registry = NodeRegistry(
        pairing,
        NodeRepository(core.db),
        signer=signer,
        core_id=lambda: holder[0].core_id if holder else "",
    )
    runtime.registry = registry

    def tls() -> TlsMaterial | None:
        material: TlsMaterial | None = lan.tls
        return material

    async def security_kill(device_id: str, reason: str) -> None:
        # Stopping is always safe, so a node may always stop everything (a compromised node can
        # at worst cause a denial of service the owner ends with "Fortsetzen").
        await core.security.killswitch.engage(
            origin="node", reason=f"node {device_id[:8]}: {reason}"[:200]
        )

    controls: list[NodeControl] = []
    on_connected = connection_hook(core, registry, controls)

    listener = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(core.db),
        tls=tls,
        host=cfg.host,
        port=cfg.node_port,
        audit=core.security.audit,
        on_security_kill=security_kill,
        allow_public_clients=cfg.allow_public_clients,
        kill_engaged=core.security.killswitch.is_engaged,
        receipt_key=signer.public_key,
        on_connected=on_connected,
        on_disconnected=registry.on_disconnected,
        on_tools_changed=registry.on_tools_changed,
    )
    runtime.listener = listener
    holder.append(listener)
    satellites = install_satellites(core, runtime, registry, listener, pairing)

    async def stop_nodes() -> None:
        # The stop reaches every node over the sockets that are already open (spec C §1.7).
        await listener.broadcast_kill("kill switch")

    async def resume_nodes() -> None:
        await listener.broadcast_resume()

    killswitch = core.security.killswitch
    killswitch.register_stop_hook("nodes", stop_nodes)
    killswitch.register_resume_hook("nodes", resume_nodes)
    control = _install_routes(core, registry, [listener])
    controls.append(control)
    # The PC Nox controls cannot talk to it (`controlled_here`).
    satellites.controlled = control.locked
    _keep(runtime, _watch_sessions(control, core, listener, satellites), "nox-node-computer")

    async def redeem(body: NodePairRequest) -> tuple[int, dict[str, Any]]:
        if not listener.running:
            return 503, {"error": "starting"}
        redeemed = await pairing.redeem(body)
        result, issued = redeemed.result, redeemed.issued
        if not result.ok or issued is None:
            return _STATUS.get(result.reason, 401), {"error": result.reason}
        answer = NodePairResponse(
            node_id=result.device_id,
            name=result.name,
            cert=issued.cert_pem,
            ca_cert=issued.ca_pem,
            core_id=listener.core_id,
            node_port=listener.port,
            receipt_key=signer.public_key,
        )
        return 201, answer.model_dump()

    lan.node_pair = redeem
    _keep(runtime, _serve(runtime, listener, lan), "nox-node-listener")
    return runtime


async def _serve(runtime: NodeRuntime, listener: NodeListener, lan: Any) -> None:
    """Start once the network listener has certificates, then follow its re-issues."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TLS_WAIT_S
    while lan.tls is None or not lan.running:
        if loop.time() > deadline:
            runtime.error = "network access did not start"
            log.error("node.listener_not_started", reason=runtime.error)
            return
        await asyncio.sleep(0.1)
    try:
        await listener.start()
    except (OSError, RuntimeError) as exc:
        runtime.error = f"{type(exc).__name__}: {exc}"
        log.error("node.listener_failed", error=runtime.error)
        return
    while True:
        await asyncio.sleep(TLS_RECHECK_S)
        try:
            await listener.reload()
        except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad round
            log.error("node.reload_failed", error=f"{type(exc).__name__}: {exc}")


def _keep(runtime: NodeRuntime, coro: Any, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    runtime.tasks.add(task)
    task.add_done_callback(runtime.tasks.discard)


#: Longest permission target a node call may have. Longer is refused, never cut: a target the
#: person cannot read in full is a target that can hide its dangerous tail (padding).
MAX_TARGET_CHARS = 1000


def _complete(text: str) -> str:
    if len(text) > MAX_TARGET_CHARS:
        raise CannotShow(f"the call is too long to be shown in full ({len(text)} characters)")
    return text


def _shell_target(payload: dict[str, Any]) -> str:
    """What a node's `shell.run` call runs, as the confirmation and the audit show it - in full or
    not at all (`CannotShow`), rendered like the device's own dialog (`protocol.render`): every
    argv token quoted on its own, a command line quoted as one value, the whole egress list.
    Never this core's view of where the program lives, which says nothing about the device."""
    argv = payload.get("argv")
    if isinstance(argv, list):
        words = "argv " + render([str(a) for a in argv])
    else:
        words = "command " + render(str(payload.get("command") or ""))
    egress = payload.get("egress") or []
    hosts = render([str(h) for h in egress]) if egress else "[none]"
    return _complete(f"{words} egress {hosts}")


def _write_target(payload: dict[str, Any]) -> str:
    """A node write: where, how (new file / overwrite / append), how much, how it starts. A
    write whose text the node would refuse anyway is refused before this - in the route's
    `prepare`, off the event loop (`_write_precheck`)."""
    shown = write_summary(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
        overwrite=bool(payload.get("overwrite")),
    )
    return _complete(" ".join(f"{key} {render(value)}" for key, value in shown.items()))


def _write_precheck(payload: dict[str, Any]) -> str | None:
    return write_text_refusal(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
    )


def _write_risk(payload: dict[str, Any]) -> Risk:
    path = str(payload.get("path") or "")
    return Risk(write_risk(path, bool(payload.get("append")), bool(payload.get("overwrite"))))


def _field(name: str) -> Callable[[dict[str, Any]], str]:
    return lambda payload: _complete(render(str(payload.get(name) or "")))


_Target = Callable[[dict[str, Any]], str]


async def send_games(core: Any, session: Any) -> None:
    """The core's watched games, for the node's game lock - an addition there, never a removal."""
    names = watched_games(core)
    if not names:
        return
    try:
        await session.client.request("nox/games", {"add": names[:64]}, timeout_s=5)
    except (TimeoutError, McpClientError) as exc:
        log.warning("node.games_failed", device_id=session.device_id, error=str(exc))


def connection_hook(
    core: Any, registry: NodeRegistry, controls: list[NodeControl]
) -> Callable[[Any], Awaitable[None]]:
    """What happens on every new authenticated node connection: its hello resets how computer
    use stands there (`NodeControl.hello`), the registry lists its tools, and it gets the core's
    watched games."""

    async def on_connected(session: Any) -> None:
        for control in controls:
            control.hello(session.device_id, session.computer)
        await registry.on_connected(session)
        await send_games(core, session)

    return on_connected


def watched_games(core: Any) -> list[str]:
    sensors = getattr(core.config, "sensors", None)
    return list(sensors.game.process_names)[:64] if sensors is not None else []


async def resend_games_if_changed(core: Any, listener: Any, sent: list[list[str]]) -> bool:
    """The core's watched games changed (settings): every connected node gets them again (the
    node only ever adds). True when they were sent."""
    games = watched_games(core)
    if sent and sent[0] == games:
        return False
    sent[:] = [games]
    for session in list(listener.sessions.values()):
        await send_games(core, session)
    return True


async def _watch_sessions(
    control: NodeControl, core: Any, listener: Any, satellites: SatelliteHub | None = None
) -> None:
    """Every second: the nodes' latest reports, so the lock, the grant and the dashboard follow
    a session that ends on the node (time, stop hotkey, kill) without another call; the core's
    watched games, sent again when they change; and the satellites' microphone gate and overlay
    credentials, which follow the privacy settings and the Devices page."""
    sent: list[list[str]] = [watched_games(core)]  # sent on every connection already
    while True:
        await asyncio.sleep(1.0)
        try:
            control.active()
            await resend_games_if_changed(core, listener, sent)
            if satellites is not None:
                await satellites.push_gates()
                await satellites.sweep()
        except Exception as exc:  # noqa: BLE001 - the watcher outlives one bad round
            log.error("node.computer_watch_failed", error=type(exc).__name__)


#: How long the hub may reuse its answer to "may this node hold an overlay credential?".
OVERLAY_CHECK_TTL_S = 2.0


class TtlCache:
    """`read(key)` remembered for `ttl_s`; `forget(key)` drops one answer at once."""

    def __init__(
        self,
        read: Callable[[str], bool],
        *,
        ttl_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read, self._ttl, self._clock = read, ttl_s, clock
        self._answers: dict[str, tuple[float, bool]] = {}

    def __call__(self, key: str) -> bool:
        now = self._clock()
        cached = self._answers.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        value = bool(self._read(key))
        self._answers[key] = (now, value)
        return value

    def forget(self, key: str) -> None:
        self._answers.pop(key, None)


def install_satellites(
    core: Any, runtime: NodeRuntime, registry: NodeRegistry, listener: Any, pairing: NodePairing
) -> SatelliteHub:
    """The core's side of the satellites (C9, `nox.node.satellites`): voice turns from nodes,
    their speech levels for a pet, overlay credentials for their overlay relays, the microphone
    gate (pushed on connection, every second, and at once on a privacy change), and the panic
    button for OBS on nodes."""
    from nox.core.events import E, Event  # noqa: PLC0415
    from nox.security._events import publish_nowait  # noqa: PLC0415

    security = getattr(core, "security", None)

    def entry_of(device_id: str) -> NodeEntry | None:
        return next((e for e in registry.known() if e.device_id == device_id), None)

    def offers(device_id: str, tool: str) -> bool:
        entry = entry_of(device_id)
        return entry is not None and entry.offers(tool)

    def enabled_on_device(device_id: str, group: str) -> bool:
        entry = entry_of(device_id)
        if entry is None or not entry.online:
            return False
        if any(group_of(tool) == group for tool in entry.tools):
            return True
        return group in entry.narrowed and registry.policy.state(device_id, group)[0]

    def capture_refusal() -> str | None:
        killswitch = getattr(security, "killswitch", None)
        if killswitch is not None and killswitch.is_engaged():
            return "stopped"
        privacy = getattr(security, "privacy", None)
        if privacy is None:
            return "no privacy settings"
        try:
            allowed = bool(privacy.allows_capture("microphone"))
        except Exception:  # noqa: BLE001 - cannot tell: closed
            return "unknown"
        return None if allowed else "microphone off (privacy settings, panic or safe mode)"

    orchestrator = getattr(core, "orchestrator", None)

    async def turn(text: str, language: str, on_chunk: Any, channel: str, note: str) -> Any:
        assert orchestrator is not None
        # Waits for the turn in flight instead of cancelling it (and a question it may wait on),
        # and interrupts no speech here; the conversation counts as tainted from its first turn
        # (`nox.security.taint.UNTRUSTED_CONVERSATIONS`).
        return await orchestrator.handle_text(
            text,
            language=language,
            speak=False,
            on_chunk=on_chunk,
            channel=channel,
            barge_in=False,
            context_note=note,
        )

    def label_of(device_id: str) -> str:
        entry = entry_of(device_id)
        return entry.label if entry is not None else device_id[:8]

    def publish(name: str, payload: dict[str, Any]) -> None:
        publish_nowait(getattr(core, "bus", None), name, payload, source="node")

    def read_overlay_allowed(device_id: str) -> bool:
        """May this node hold an overlay credential (blocking: the database)? Paired, not
        removed, `obs` enabled on the device as it last said and not narrowed here."""
        device = pairing.get(device_id)
        if device is None or device.revoked:
            return False
        on_device, narrowed = registry.policy.state(device_id, "obs")
        return on_device and not narrowed

    # The hub asks at every use of a node's overlay credential, on its own loop: a short cache,
    # emptied for a node the moment it is narrowed or removed.
    overlay_allowed = TtlCache(read_overlay_allowed, ttl_s=OVERLAY_CHECK_TTL_S)

    tokens = getattr(core, "tokens", None)
    if tokens is not None and hasattr(tokens, "node_overlay_allowed"):
        # The hub asks again whenever a node's overlay credential is used over the network.
        tokens.node_overlay_allowed = overlay_allowed
    hub = getattr(core, "hub", None)
    satellites = SatelliteHub(
        offers=offers,
        enabled_on_device=enabled_on_device,
        sessions=lambda: list(listener.sessions.values()),
        turn=turn if orchestrator is not None else None,
        capture_refusal=capture_refusal,
        publish=publish,
        audit=getattr(security, "audit", None),
        overlay=getattr(tokens, "overlay", None),
        disconnect_credential=getattr(hub, "disconnect_credential", None),
        overlay_allowed=overlay_allowed,
        opted_in=registry.policy.opted_in,
        label=label_of,
    )
    listener.on_satellite = satellites.handle
    engine = getattr(security, "engine", None)
    if engine is not None and hasattr(engine, "satellite_label"):
        engine.satellite_label = label_of  # "von Sprach-Satellit <name>" in its questions
    connected, disconnected = listener._on_connected, listener._on_disconnected  # noqa: SLF001

    def panicking() -> bool:
        privacy = getattr(security, "privacy", None)
        try:
            return bool(getattr(privacy, "panic", False))
        except Exception:  # noqa: BLE001 - cannot tell: protective
            return True

    async def on_connected(session: Any) -> None:
        if connected is not None:
            await connected(session)
        await satellites.push_gates()
        if panicking():
            # Offline when the panic button was pressed: its OBS goes to the privacy scene now.
            await satellites.panic("panic", sessions=[session])

    async def on_disconnected(session: Any) -> None:
        satellites.disconnected(session.device_id)
        if disconnected is not None:
            await disconnected(session)

    listener._on_connected = on_connected  # noqa: SLF001
    listener._on_disconnected = on_disconnected  # noqa: SLF001

    async def forget(device_id: str, reason: str) -> None:
        overlay_allowed.forget(device_id)
        await satellites.revoke_overlay(device_id, f"node removed: {reason}")

    pairing.on_revoked(forget)

    async def narrowed(device_id: str, group: str, on: bool) -> None:
        if group == "obs":
            overlay_allowed.forget(device_id)
        if group == "obs" and on:
            # Switched off in the Devices page: its overlay goes dark now, online or not.
            await satellites.revoke_overlay(device_id, "obs narrowed")

    registry.on_narrowed.append(narrowed)
    _keep(runtime, satellites.sweep(), "nox-node-overlay-sweep")  # stale ones from earlier runs
    bus = getattr(core, "bus", None)
    if bus is not None:

        async def gates(_event: Event) -> None:
            await satellites.push_gates()

        async def panic(_event: Event) -> None:
            await satellites.push_gates()
            await satellites.panic("panic")

        for name in (E.PRIVACY_CAPTURE_CHANGED, E.SECURITY_KILL_SWITCH, E.SYSTEM_STARTED):
            bus.subscribe(name, gates)
        bus.subscribe(E.SECURITY_PANIC, panic)
    runtime.satellites = satellites
    return satellites


def _install_routes(
    core: Any, registry: NodeRegistry, listeners: list[Any] | None = None
) -> NodeControl:
    """`system.info` (the core has it too), `notify.show` (nodes only) and the hands - `file.*`
    and `shell.run`, each the core's own tool when the call stays here - become routable; the
    capability report hides a node-only tool while no node offers it, and every turn's context
    names the devices. `package.*` stays on the core: it cannot work in a node's sandbox yet."""
    defaults = lambda: core.config.devices.defaults  # noqa: E731 - read per call
    own = core.tool_registry.get
    hands: list[tuple[str, str, Risk, bool, type[BaseModel], _Target]] = [
        ("file.list", "What is in one folder", Risk.READ, False, PathArgs, _field("path")),
        ("file.read", "The text of one file", Risk.READ, False, PathArgs, _field("path")),
        ("file.write", "Write text to a file", Risk.MEDIUM, True, WriteArgs, _write_target),
        ("shell.run", "Run a command sandboxed", Risk.HIGH, True, ShellArgs, _shell_target),
    ]
    specs = [
        routed_spec(
            name="system.info",
            description="System values of a device: OS, CPU, memory, disk, battery, "
            "temperature, uptime. On this core or on a paired PC (`device`).",
            risk=Risk.READ,
            side_effects=False,
            registry=registry,
            core=core_system_info_spec(),
            defaults=defaults,
        ),
        routed_spec(
            name="notify.show",
            description="Show a notification on a paired PC or laptop (`device`).",
            risk=Risk.LOW,
            side_effects=True,
            registry=registry,
            input_model=NotifyArgs,
            defaults=defaults,
        ),
    ]
    for name, what, risk, side_effects, model, target in hands:
        mine = own(name)
        specs.append(
            routed_spec(
                name=name,
                description=(mine.description if mine is not None else what + ".")
                + " On this core or on a paired PC (`device`).",
                risk=risk,
                side_effects=side_effects,
                registry=registry,
                core=mine,
                input_model=model,
                defaults=defaults,
                node_target=target,
                # Replacing or extending a file on a device is high there, and a write on a
                # device is asked every time (never remembered).
                node_risk=_write_risk if name == "file.write" else None,
                remember_on_node=name != "file.write",
                precheck=_write_precheck if name == "file.write" else None,
            )
        )
    world, control = _world_specs(core, registry, defaults, listeners or [])
    specs += world
    engine = getattr(getattr(core, "security", None), "engine", None)

    def tainted_now() -> bool:
        from nox.security.taint import current_conversation  # noqa: PLC0415

        try:
            return engine is None or bool(engine.is_tainted(current_conversation()))
        except Exception:  # noqa: BLE001 - cannot tell: tainted
            return True

    specs.append(voice_say_spec(registry, defaults, tainted=tainted_now))  # C9: speak on a node
    on_core = {spec.name for spec in specs if own(spec.name) is not None}
    install_routes(core.tool_registry, specs)
    node_only = {spec.name for spec in specs} - on_core
    tools = core.tool_registry
    wrapped: set[str] = set()
    if hasattr(tools, "wrap"):
        # C9: `obs.request` - the plugin's tool while OBS runs here, routed to the node where OBS
        # runs otherwise. The plugin comes and goes; the route stays (`ToolRegistry.wrap`).
        tools.wrap("obs.request", obs_route(registry, defaults))
        wrapped.add("obs.request")

    def probe(tool_name: str) -> tuple[Any, str] | None:
        node_only_now = tool_name in node_only or (
            tool_name in wrapped and tools.own(tool_name) is None
        )
        if node_only_now and not registry.any_online_with(tool_name):
            from nox.capabilities.model import CapabilityState  # noqa: PLC0415

            return CapabilityState.UNAVAILABLE, "no paired device with this is online"
        return None

    core.node_probe = probe
    orchestrator = getattr(core, "orchestrator", None)
    if orchestrator is not None:
        base = orchestrator.facts

        def facts() -> str:
            text = base() if base is not None else ""
            line = registry.prompt_line()
            return f"{text}\n{line}" if text and line else (text or line)

        orchestrator.facts = facts
    return control


def _world_specs(
    core: Any, registry: NodeRegistry, defaults: Callable[[], Any], listeners: list[Any]
) -> tuple[list[ToolSpec], NodeControl]:
    """C6: captures, browser, desktop, computer use and media, routable to nodes; and the
    record of computer-use sessions on nodes (dashboard notice, grant, "allow" lock)."""
    from nox.security._events import publish_nowait  # noqa: PLC0415

    def publish(payload: dict[str, Any]) -> None:
        publish_nowait(getattr(core, "bus", None), "desktop.computer_use", payload, source="node")

    async def end_on_node(entry: NodeEntry) -> dict[str, Any]:
        answer: dict[str, Any] = await registry.call(
            entry, "computer.session_end", {}, risk="low", timeout_s=10
        )
        return answer

    security = getattr(core, "security", None)
    desktop = getattr(core.config, "desktop", None)

    def reports() -> list[tuple[str, Any]]:
        listener = listeners[0] if listeners else None
        if listener is None:
            return []
        return [(d, s.computer) for d, s in listener.sessions.items() if s.computer is not None]

    def linked(device_id: str) -> str | None:
        entry = next((e for e in registry.known() if e.device_id == device_id), None)
        return entry.linked_device_id if entry is not None else None

    def label_of(device_id: str) -> str:
        entry = next((e for e in registry.entries() if e.device_id == device_id), None)
        return entry.label if entry is not None else device_id[:8]

    control = NodeControl(
        getattr(security, "engine", None),
        publish,
        end_on_node=end_on_node,
        reports=reports,
        labels=label_of,
        linked=linked,
        stop_hotkey=desktop.computer_use.stop_hotkey if desktop is not None else "",
    )
    wire_guards(core, control)
    # A stop never lifts the lock by itself: the node ends its session on `nox/kill` and says
    # so in its next pong, which `pull` reads (also every second, `_watch_sessions`).
    paths = getattr(core.config, "paths", None)
    captures = Path(paths.runtime_dir if paths is not None else ".") / "hands" / "captures"
    specs = world_specs(
        registry=registry,
        own=core.tool_registry.get,
        defaults=defaults,
        control=control,
        captures_root=lambda: captures,
        privacy=lambda: getattr(security, "privacy", None),
    )
    return specs, control


def wire_guards(core: Any, control: NodeControl) -> None:
    """While Nox controls a node: every "allow" (`security.permission.reply`) and every request
    that changes something (`RequestRegistry.node_guard`) go through `session_refusal`; and the
    `node.computer.*` requests. Separate so a test can wire a fake core exactly like this."""
    security = getattr(core, "security", None)

    def gate() -> Any:
        return getattr(security, "gate", None)

    async def guard(ctx: Any, pin: str | None, action: str) -> str | None:
        return await session_refusal(
            control,
            ctx,
            pin,
            action=action,
            gate=gate(),
            device_guard=getattr(core, "device_pin_guard", None),
            remote=console_is_remote,
        )

    async def approval(ctx: Any, pin: str | None) -> str | None:
        return await guard(ctx, pin, "security.permission.reply")

    core.node_approval_guard = approval
    ipc = getattr(core, "registry", None)
    if ipc is not None and hasattr(ipc, "register"):
        ipc.node_guard = guard
        _register_control_requests(core, ipc, control, gate)


class ReleaseRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)


def _register_control_requests(core: Any, ipc: Any, control: NodeControl, gate: Any) -> None:
    """`node.computer.status` (what the header shows, for a dashboard that (re)connects) and
    `node.computer.release` (lift a stuck lock by hand: only at the computer Nox runs on, with
    the PIN, not remotely; a node that still reports a session locks again at once)."""
    from nox.ipc.roles import LOCAL_UI_ROLES  # noqa: PLC0415

    async def h_status(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        return {"sessions": control.status()}

    async def h_release(ctx: RequestContext, p: ReleaseRequest) -> dict[str, Any]:
        refusal = await local_pin_refusal(
            ctx, p.pin, gate=gate(), remote=console_is_remote, action="node.computer.release"
        )
        if refusal is not None:
            core.security.audit.append(
                actor=ctx.client_id or ctx.role,
                tool="node",
                action="node.computer.release",
                target="",
                decision="deny",
                result=refusal[:64],
            )
            raise IpcError(ERR_UNAVAILABLE, refusal, details={"reason": "refused"})
        released = control.release("released by the owner")
        core.security.audit.append(
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


def core_system_info_spec() -> ToolSpec:
    async def handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return await system_info(NoArgs())

    return ToolSpec(
        name="system.info",
        description="System values of this machine.",
        input_model=NoArgs,
        risk=Risk.READ,
        handler=handler,
        side_effects=False,
    )


def _node_json(device: DeviceRow, runtime: NodeRuntime) -> dict[str, Any]:
    session = runtime.listener.sessions.get(device.id) if runtime.listener else None
    entry = (
        next((e for e in runtime.registry.entries() if e.device_id == device.id), None)
        if runtime.registry is not None
        else None
    )
    return {
        "label": label(device.name, device.id),
        "tools": sorted(entry.tools) if entry is not None and entry.online else [],
        "linked_device_id": entry.linked_device_id if entry is not None else None,
        "id": device.id,
        "name": device.name,
        "paired_at": device.paired_at.isoformat(),
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "revoked": device.revoked,
        "online": session is not None,
        "compatible": session.compatible if session is not None else None,
        "os": session.hello.os if session is not None else None,
        "version": session.hello.node_version if session is not None else None,
    }


def _register_requests(core: Any, runtime: NodeRuntime, lan: Any) -> None:
    pairing = runtime.pairing

    def require_running() -> tuple[TlsMaterial, NodeListener]:
        listener = runtime.listener
        if not runtime.enabled or listener is None or lan is None:
            raise IpcError(
                ERR_UNAVAILABLE,
                "nodes need network access (ipc.lan.enabled)",
                details={"reason": "off"},
            )
        material = lan.tls
        if material is None or not listener.running:
            raise IpcError(
                ERR_UNAVAILABLE,
                runtime.error or "the node listener is starting",
                retryable=not runtime.error,
                details={"reason": "failed" if runtime.error else "starting"},
            )
        return material, listener

    async def h_pair_start(ctx: RequestContext, p: PairStartRequest) -> dict[str, Any]:
        material, _listener = require_running()
        await authorize_admin(core, ctx, p.pin, "node.pair", local_needs_pin=True)
        hosts = [a for a in material.addresses if a != "127.0.0.1"]
        hosts += [n for n in material.names if n != "localhost"]
        if not hosts:
            raise IpcError(
                ERR_UNAVAILABLE,
                "this machine has no address in the home network",
                details={"reason": "no_address"},
            )
        start = await pairing.start(name=p.name)
        links = [
            PairLink.make(
                host=h, port=lan.port, code=start.code, ca_fingerprint=material.fingerprint
            ).to_url()
            for h in hosts
        ]
        return {
            "code": start.code,
            "expires_at": start.expires_at.isoformat(),
            "ttl_s": start.ttl_s,
            "link": links[0],
            "links": links,
            "qr": qr_data_uri(links[0]),
            "ca_fingerprint": material.fingerprint,
        }

    details = register_policy_requests(core, runtime)

    async def h_list(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        listed = await asyncio.to_thread(pairing.nodes)
        online = set(runtime.listener.sessions) if runtime.listener is not None else set()
        extra = await asyncio.to_thread(details, listed, online)
        return {
            "enabled": runtime.enabled,
            "nodes": [_node_json(d, runtime) | extra[d.id] for d in listed],
            "defaults": defaults_view(core.config.devices.defaults, listed),
        }

    async def h_unpair(ctx: RequestContext, p: UnpairRequest) -> dict[str, Any]:
        if pairing.get(p.device_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such node")
        await authorize_admin(core, ctx, p.pin, "node.unpair", local_needs_pin=False)
        online = runtime.listener is not None and p.device_id in runtime.listener.sessions
        ok = await pairing.revoke(p.device_id, reason="dashboard")
        return {"ok": ok, "device_id": p.device_id, "was_online": online}

    reg = core.registry.register
    reg("node.pair.start", PairStartRequest, h_pair_start, roles=UI_ROLES)
    reg("node.list", EmptyRequest, h_list, roles=UI_ROLES)
    reg("node.unpair", UnpairRequest, h_unpair, roles=UI_ROLES)

    async def h_link(ctx: RequestContext, p: LinkRequest) -> dict[str, Any]:
        """Which browser device sits on this node's machine (for `origin` routing). Changing it
        decides where a request from that browser lands, so it is administration: the PIN from a
        paired device, as for pairing."""
        if pairing.get(p.node_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such node")
        if p.device_id is not None:
            browser = core.lan_runtime.devices.get(p.device_id) if lan is not None else None
            if browser is None or browser.revoked:
                raise IpcError(ERR_NOT_FOUND, "no such paired device")
        await authorize_admin(core, ctx, p.pin, "node.link", local_needs_pin=True)
        await asyncio.to_thread(NodeRepository(core.db).set_linked_device, p.node_id, p.device_id)
        return {"ok": True, "node_id": p.node_id, "device_id": p.device_id}

    reg("node.link", LinkRequest, h_link, roles=UI_ROLES)
