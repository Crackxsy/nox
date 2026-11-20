"""The requests that pair, list and remove nodes - and `POST /api/node/pair`, where a node pairs.

Registered always, also while network access is off, so the dashboard can say honestly why nodes
are unavailable (`node.list` answers `enabled: false`; `node.pair.start` says why it cannot).
Owns the request models and the handlers of `node.pair.start`, `node.list`, `node.unpair` and
`node.link`, and the redeem function behind the pairing route. What a node may do is in
`nox.node.hub.policy`; the node listener itself is `nox.node.hub.listener`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_NOT_FOUND, ERR_UNAVAILABLE, IpcError
from nox.ipc.roles import UI_ROLES
from nox.lan.install import authorize_admin, qr_data_uri
from nox.lan.tls import TlsMaterial
from nox.node.hub.install.runtime import NodeRuntime
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.hub.policy import defaults_view, register_policy_requests
from nox.node.hub.ports import CoreServices, LanPort, NodePairHandler
from nox.node.hub.repo import NodeRepository
from nox.node.wire.names import label
from nox.node.wire.protocol import NodePairRequest, NodePairResponse, PairLink
from nox.node.wire.receipts import ReceiptSigner
from nox.remote.models import DeviceRow

__all__ = ["EmptyRequest", "pair_route", "register_requests"]


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


def pair_route(
    listener: NodeListener, pairing: NodePairing, signer: ReceiptSigner
) -> NodePairHandler:
    """What `POST /api/node/pair` runs: redeem the code and answer with the node's certificate,
    the authority and where to connect - or the HTTP status of why not."""

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

    return redeem


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
        # The folders the device allowed for `files`, as it last said (only the device adds one).
        "folders": list(entry.folders) if entry is not None and entry.online else [],
        "compatible": session.compatible if session is not None else None,
        "os": session.hello.os if session is not None else None,
        "version": session.hello.node_version if session is not None else None,
    }


def register_requests(services: CoreServices, runtime: NodeRuntime, lan: LanPort | None) -> None:
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
        assert lan is not None  # `require_running` refuses without it
        await authorize_admin(services, ctx, p.pin, "node.pair", local_needs_pin=True)
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

    details = register_policy_requests(services, runtime)

    async def h_list(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        listed = await asyncio.to_thread(pairing.nodes)
        online = set(runtime.listener.sessions) if runtime.listener is not None else set()
        extra = await asyncio.to_thread(details, listed, online)
        return {
            "enabled": runtime.enabled,
            "nodes": [_node_json(d, runtime) | extra[d.id] for d in listed],
            "defaults": defaults_view(services.config.devices.defaults, listed),
        }

    async def h_unpair(ctx: RequestContext, p: UnpairRequest) -> dict[str, Any]:
        if pairing.get(p.device_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such node")
        await authorize_admin(services, ctx, p.pin, "node.unpair", local_needs_pin=False)
        online = runtime.listener is not None and p.device_id in runtime.listener.sessions
        ok = await pairing.revoke(p.device_id, reason="dashboard")
        return {"ok": ok, "device_id": p.device_id, "was_online": online}

    reg = services.registry.register
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
            browser = lan.devices.get(p.device_id) if lan is not None else None
            if browser is None or browser.revoked:
                raise IpcError(ERR_NOT_FOUND, "no such paired device")
        await authorize_admin(services, ctx, p.pin, "node.link", local_needs_pin=True)
        registry = runtime.registry
        if registry is not None:
            # The one writer: the database and what routing reads from memory, together.
            await registry.set_linked(p.node_id, p.device_id)
        else:
            await asyncio.to_thread(
                NodeRepository(services.db).set_linked_device, p.node_id, p.device_id
            )
        return {"ok": True, "node_id": p.node_id, "device_id": p.device_id}

    reg("node.link", LinkRequest, h_link, roles=UI_ROLES)
