"""`install(core) -> NodeRuntime`: the core's side of `nox node`, wired into a started core.

The requests are registered always, so the dashboard can say honestly why nodes are unavailable;
the node listener (`nox.node.hub.listener`) only starts with network access (`ipc.lan.enabled`),
because it needs the installation's authority and its server certificate.

Requests, for the UI roles (`nox.ipc.roles`):
  * `node.pair.start` - a one-time code and the `nox-node://pair?...` link (and its QR code) that
    carries the authority's fingerprint. Pairing a node gives a PC to Nox's hands, so it needs the
    PIN when one is set - and from a paired device always (`nox.lan.install.authorize_admin`),
  * `node.list` - paired nodes, online or not,
  * `node.unpair` - revoke a node: its certificates go on the denylist, its connection closes
    at once, and a computer-use lock it held ends with it. Narrowing only, so no PIN locally -
    also while Nox controls a PC (`nox.node.hub.session_access.ESCAPES`).
  * `node.narrow`, `node.defaults.set` - see `nox.node.hub.policy`.

How the package is cut: `ports` (what this extension needs from the core, typed) is the only
reader of the core; `pairing_requests`, `routes`, `satellites` and `control` each wire one part
onto the ports; this module is the composition root and the one public entry. It hangs onto the
core only through the core's extension points - named lists that other extensions can join
(`Orchestrator.add_fact_source`, `ToolRegistry.add_availability_probe`,
`PermissionEngine.add_satellite_label`, `TokenStore.add_node_overlay_check`, the killswitch's
stop and resume hooks) - and onto its own listener through `NodeListener.on_connected` and
`on_disconnected`, whose order is set in `install` below.
"""

from __future__ import annotations

from pathlib import Path

from nox.core.logging import get_logger
from nox.node.hub.install.control import attach_control, connection_hook, watch_sessions
from nox.node.hub.install.pairing_requests import pair_route, register_requests
from nox.node.hub.install.routes import install_node_routes
from nox.node.hub.install.runtime import NodeRuntime, keep, serve
from nox.node.hub.install.satellites import install_satellites
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.ports import HOOK_NAME, CoreLike, CoreServices, NodeWiringError
from nox.node.hub.registry import NodeRegistry
from nox.node.hub.repo import NodeRepository
from nox.node.wire.receipts import ReceiptSigner
from nox.remote.repo import RemoteRepository

log = get_logger(__name__)

__all__ = ["NodeRuntime", "install"]


def install(core: CoreLike) -> NodeRuntime:
    """Wire the node extension onto a started core; see the module docstring."""
    services = CoreServices.of(core)
    _refuse_a_second_installation(services)
    cfg = services.config.ipc.lan
    lan = services.lan
    tls_dir = Path(services.config.paths.data_dir) / "tls"
    pairing = NodePairing(
        RemoteRepository(services.db),
        NodeRepository(services.db),
        tls_dir=tls_dir,
        bus=services.bus,
        audit=services.security.audit,
        ttl_s=cfg.pair_code_ttl_s,
        max_nodes=cfg.max_nodes,
    )
    runtime = NodeRuntime(pairing=pairing, enabled=bool(cfg.enabled) and lan is not None)

    async def forget_grants(device_id: str, reason: str) -> None:
        # A removed node keeps no remembered "allow" that a re-paired one could inherit.
        services.security.engine.revoke_device_grants(
            device_id, reason=f"node removed: {reason}"[:64]
        )

    pairing.on_revoked(forget_grants)

    register_requests(services, runtime, lan)
    if not runtime.enabled or lan is None:
        return runtime

    # The core signs every call with a side effect; nodes pin the public half at pairing.
    signer = ReceiptSigner(tls_dir)

    def core_id() -> str:
        # The registry is built before the listener whose id this is: "" until it exists.
        return runtime.listener.core_id if runtime.listener is not None else ""

    registry = NodeRegistry(
        pairing,
        NodeRepository(services.db),
        signer=signer,
        core_id=core_id,
    )
    runtime.registry = registry

    async def security_kill(device_id: str, reason: str) -> None:
        # Stopping is always safe, so a node may always stop everything (a compromised node can
        # at worst cause a denial of service the owner ends with "Fortsetzen").
        await services.security.killswitch.engage(
            origin="node", reason=f"node {device_id[:8]}: {reason}"[:200]
        )

    listener = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(services.db),
        tls=lambda: lan.tls,
        host=cfg.host,
        port=cfg.node_port,
        audit=services.security.audit,
        on_security_kill=security_kill,
        allow_public_clients=cfg.allow_public_clients,
        kill_engaged=services.security.killswitch.is_engaged,
        receipt_key=signer.public_key,
        on_tools_changed=registry.on_tools_changed,
    )
    runtime.listener = listener
    satellites = install_satellites(services, registry, listener, pairing)
    runtime.satellites = satellites.hub
    keep(runtime, satellites.hub.sweep(), "nox-node-overlay-sweep")  # stale ones from earlier runs

    async def stop_nodes() -> None:
        # The stop reaches every node over the sockets that are already open.
        await listener.broadcast_kill("kill switch")

    async def resume_nodes() -> None:
        await listener.broadcast_resume()

    killswitch = services.security.killswitch
    killswitch.register_stop_hook(HOOK_NAME, stop_nodes)
    killswitch.register_resume_hook(HOOK_NAME, resume_nodes)
    control = install_node_routes(services, registry, listener)
    runtime.control = control

    async def lift_lock(device_id: str, _reason: str) -> None:
        # Removed: whatever lock, address binding or saturation it held ends now.
        control.forget(device_id)

    pairing.on_revoked(lift_lock)
    # The PC Nox controls cannot talk to it (`controlled_here`).
    attach_control(satellites.hub, control)
    # What happens when a node connects and when it goes, in this order and no other: the node
    # is vetted and listed first (a hello that claims a computer-use session ends the connection
    # here), then its microphone gate and a panic that is on go out. On the way out the satellites
    # forget the node while the registry still lists it online, then the registry marks it offline.
    listener.on_connected.append(connection_hook(services.config, registry, control, listener))
    listener.on_connected.append(satellites.on_connected)
    listener.on_disconnected.append(satellites.on_disconnected)
    listener.on_disconnected.append(registry.on_disconnected)
    keep(
        runtime,
        watch_sessions(control, services.config, listener, satellites.hub),
        "nox-node-computer",
    )
    lan.node_pair = pair_route(listener, pairing, signer)
    keep(runtime, serve(runtime, listener, lan), "nox-node-listener")
    return runtime


def _refuse_a_second_installation(services: CoreServices) -> None:
    """Installing twice would register the requests, hooks and routes twice, or half of them: say
    so before anything is wired."""
    if services.registry.get("node.list") is not None or (
        services.lan is not None and services.lan.node_pair is not None
    ):
        raise NodeWiringError("the node extension is already installed on this core")
