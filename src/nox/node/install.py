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
from collections.abc import Callable
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
from nox.node.policy import defaults_view, register_policy_requests
from nox.node.protocol import NodePairRequest, NodePairResponse, PairLink, render
from nox.node.receipts import ReceiptSigner
from nox.node.registry import NodeRegistry
from nox.node.repo import NodeRepository
from nox.node.router import CannotShow, install_routes, routed_spec
from nox.node.tools import NoArgs, NotifyArgs, system_info
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
        on_connected=registry.on_connected,
        on_disconnected=registry.on_disconnected,
        on_tools_changed=registry.on_tools_changed,
    )
    runtime.listener = listener
    holder.append(listener)

    async def stop_nodes() -> None:
        # The stop reaches every node over the sockets that are already open (spec C §1.7).
        await listener.broadcast_kill("kill switch")

    async def resume_nodes() -> None:
        await listener.broadcast_resume()

    killswitch = core.security.killswitch
    killswitch.register_stop_hook("nodes", stop_nodes)
    killswitch.register_resume_hook("nodes", resume_nodes)
    _install_routes(core, registry)

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
    write whose text the node would refuse anyway is refused here, before anyone is asked."""
    early = write_text_refusal(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
    )
    if early is not None:
        raise CannotShow(early, error=early)
    shown = write_summary(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
        overwrite=bool(payload.get("overwrite")),
    )
    return _complete(" ".join(f"{key} {render(value)}" for key, value in shown.items()))


def _write_risk(payload: dict[str, Any]) -> Risk:
    path = str(payload.get("path") or "")
    return Risk(write_risk(path, bool(payload.get("append")), bool(payload.get("overwrite"))))


def _field(name: str) -> Callable[[dict[str, Any]], str]:
    return lambda payload: _complete(render(str(payload.get(name) or "")))


_Target = Callable[[dict[str, Any]], str]


def _install_routes(core: Any, registry: NodeRegistry) -> None:
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
            )
        )
    on_core = {spec.name for spec in specs if own(spec.name) is not None}
    install_routes(core.tool_registry, specs)
    node_only = {spec.name for spec in specs} - on_core

    def probe(tool_name: str) -> tuple[Any, str] | None:
        if tool_name in node_only and not registry.any_online_with(tool_name):
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
