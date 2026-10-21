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
"""

from __future__ import annotations

import asyncio
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
from nox.node.listener import NodeListener
from nox.node.pairing import NodePairing
from nox.node.protocol import NodePairRequest, NodePairResponse, PairLink
from nox.node.repo import NodeRepository
from nox.remote.models import DeviceRow
from nox.remote.repo import RemoteRepository

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


#: Redemption reasons -> HTTP status of `POST /api/node/pair`.
_STATUS = {"bad_csr": 400, "max_devices": 409, "issue_failed": 503}


@dataclass
class NodeRuntime:
    pairing: NodePairing
    enabled: bool
    listener: NodeListener | None = None
    error: str = ""
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    @property
    def running(self) -> bool:
        return self.listener is not None and self.listener.running

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
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
    _register_requests(core, runtime, lan)
    if not runtime.enabled or lan is None:
        return runtime

    def tls() -> TlsMaterial | None:
        material: TlsMaterial | None = lan.tls
        return material

    async def security_kill(device_id: str, reason: str) -> None:
        # Stopping is always safe, so a node may always stop everything (a compromised node can
        # at worst cause a denial of service the owner ends with "Fortsetzen").
        await core.security.killswitch.engage(
            origin="node", reason=f"node {device_id[:8]}: {reason}"[:200]
        )

    listener = NodeListener(
        pairing=pairing,
        nodes=NodeRepository(core.db),
        tls=tls,
        host=cfg.host,
        port=cfg.node_port,
        audit=core.security.audit,
        on_security_kill=security_kill,
        allow_public_clients=cfg.allow_public_clients,
    )
    runtime.listener = listener

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


def _node_json(device: DeviceRow, runtime: NodeRuntime) -> dict[str, Any]:
    session = runtime.listener.sessions.get(device.id) if runtime.listener else None
    return {
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

    async def h_list(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        listed = await asyncio.to_thread(pairing.nodes)
        return {"enabled": runtime.enabled, "nodes": [_node_json(d, runtime) for d in listed]}

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
