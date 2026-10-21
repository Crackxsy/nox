"""What the dashboard may change about a node: narrow its capability groups, and say which device a
tool family runs on by default (spec C §1.10).

The device decides what Nox may do on it (`node.yaml`, `nox node allow`). From the core the owner
can only *take away* - a narrowed group is filtered out of what the core routes and sent to the
node as `nox/policy`, so both sides refuse it - and give back what they took away earlier. A group
the device has not enabled can never be switched on from here: the answer is `enable_on_device`,
and the page says how to do it on the device itself.

Which group a tool belongs to is decided here, by the tool's name, never by the node's word: a node
claiming that `shell.run` is a `system` tool cannot escape a narrowed `shell` group that way.

Requests, for the UI roles (`nox.ipc.roles`):
  * `node.narrow {node_id, group, narrowed, pin?}` - narrowing needs no PIN on this machine (it
    only takes away; from a paired device the PIN, as for removing a node); giving a group back
    widens what Nox may do again, so it needs the PIN when one is set, and from a paired device
    always.
  * `node.defaults.set {group, device, pin?}` - `device` is `auto`, `core`, `origin` or a paired
    node's id. It decides where a request lands, so it is administration like `node.link`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.core.config.core import DevicesConfig
from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_NOT_FOUND, ERR_VALIDATION, IpcError
from nox.ipc.roles import UI_ROLES
from nox.lan.install import authorize_admin
from nox.mcp.client import McpClientError
from nox.node.manifest import GROUPS
from nox.node.repo import NodeRepository
from nox.remote.models import DeviceRow

log = get_logger(__name__)

__all__ = [
    "AUTO",
    "DEFAULT_WORDS",
    "NodePolicy",
    "family_tools",
    "group_of",
    "register_policy_requests",
]

#: Tool name prefix -> capability group (spec C §1.4). The core's own mapping, see the docstring.
_PREFIX_GROUP: dict[str, str] = {
    "system": "system",
    "notify": "notify",
    "media": "media",
    "file": "files",
    "shell": "shell",
    "package": "packages",
    "screen": "screen",
    "camera": "camera",
    "browser": "browser",
    "desktop": "desktop",
    "computer": "computer",
    "voice": "voice",
    "obs": "obs",
}
#: "No default": the router decides as if nothing was configured (`nox.node.router.resolve`).
AUTO = "auto"
DEFAULT_WORDS: frozenset[str] = frozenset({AUTO, "core", "origin"})


def group_of(tool: str) -> str | None:
    """The capability group of a tool, by its name; None for a name outside every group."""
    return _PREFIX_GROUP.get(tool.split(".", 1)[0])


def family_tools(group: str, routable: Iterable[str], configured: Iterable[str]) -> list[str]:
    """The tools a family default applies to: those Nox routes, and those already configured."""
    return sorted({t for t in (*routable, *configured) if group_of(t) == group})


class NodePolicy:
    """The core's narrowing of each node, kept in `node_capabilities` (migration 0017)."""

    def __init__(
        self, nodes: NodeRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._nodes = nodes
        self._clock = clock or (lambda: datetime.now(UTC))

    def narrowed(self, device_id: str) -> set[str]:
        return self._nodes.narrowed_groups(device_id)

    def record_listing(self, device_id: str, tools: Mapping[str, str]) -> dict[str, str]:
        """A node's accepted tool list: remember which groups it has enabled, and return the tools
        without those of a narrowed group.

        While a group is narrowed the node no longer lists it, so its absence says nothing; what
        the device said about it before is kept. The node resets its narrowing on every new
        connection, so the first listing after connecting is always complete.
        """
        narrowed = self.narrowed(device_id)
        before = self._nodes.capabilities(device_id)
        listed = {g for g in (group_of(t) for t in tools) if g is not None}
        reported = {g: True for g in listed}
        for group in narrowed - listed:
            reported[group] = before.get(group, (False, True))[0]
        self._nodes.set_reported_groups(device_id, reported, when=self._clock())
        return {t: r for t, r in tools.items() if group_of(t) not in narrowed}

    def set_narrowed(self, device_id: str, group: str, narrowed: bool) -> None:
        self._nodes.narrow(device_id, group, narrowed, when=self._clock())

    def enabled_on_device(self, device_id: str, group: str) -> bool:
        return self._nodes.capabilities(device_id).get(group, (False, False))[0]

    def view(self, device_id: str, relevant: Iterable[str]) -> list[dict[str, Any]]:
        """Every group worth showing for one node, in the order of `GROUPS`: those Nox can route
        a tool of, and any the device reported or the core narrowed."""
        caps = self._nodes.capabilities(device_id)
        shown = set(relevant) | {g for g, (on, narrow) in caps.items() if on or narrow}
        out = []
        for group in GROUPS:
            if group not in shown:
                continue
            on, narrow = caps.get(group, (False, False))
            out.append(
                {
                    "group": group,
                    "enabled_on_device": on,
                    "narrowed": narrow,
                    "active": on and not narrow,
                }
            )
        return out


# ---- requests ------------------------------------------------------------------------------------


class NarrowRequest(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    group: str = Field(min_length=1, max_length=32)
    narrowed: bool
    pin: str | None = Field(default=None, max_length=128)


class DefaultsSetRequest(BaseModel):
    group: str = Field(min_length=1, max_length=32)
    device: str = Field(min_length=1, max_length=64)
    pin: str | None = Field(default=None, max_length=128)


def _routable() -> frozenset[str]:
    from nox.node.registry import ROUTABLE  # noqa: PLC0415 - the registry imports this module

    return ROUTABLE


def relevant_groups() -> set[str]:
    """Groups with at least one tool Nox routes to nodes in this release."""
    return {g for g in (group_of(t) for t in _routable()) if g is not None}


def _family_default(values: list[str]) -> str:
    if not values:
        return AUTO
    first = values[0]
    return first if all(v == first for v in values) else "mixed"


def defaults_view(defaults: Mapping[str, str], nodes: list[DeviceRow]) -> list[dict[str, Any]]:
    """One entry per family Nox routes: the device its tools run on when nobody says, with a
    node's name resolved to its id (`device`) when exactly one paired node has it."""
    active = [d for d in nodes if not d.revoked]
    out = []
    for group in GROUPS:
        if group not in relevant_groups():
            continue
        tools = sorted(t for t in _routable() if group_of(t) == group)
        raw = _family_default([defaults[t] for t in tools if t in defaults])
        device = raw
        if raw not in DEFAULT_WORDS and raw != "mixed":
            key = raw.casefold()
            found = [d for d in active if key in (d.id, d.name.casefold())]
            device = found[0].id if len(found) == 1 else raw
        out.append({"group": group, "tools": tools, "device": device, "raw": raw})
    return out


def register_policy_requests(core: Any, runtime: Any) -> Callable[[DeviceRow], dict[str, Any]]:
    """Register `node.narrow` and `node.defaults.set`; returns what `node.list` adds per node."""
    pairing = runtime.pairing
    nodes = NodeRepository(core.db)
    standalone = NodePolicy(nodes)

    def policy() -> NodePolicy:
        registry = runtime.registry
        return registry.policy if registry is not None else standalone

    def details(device: DeviceRow) -> dict[str, Any]:
        row = nodes.get_node(device.id)
        extra: dict[str, Any] = {
            "groups": policy().view(device.id, relevant_groups()),
            "arch": row.arch if row is not None else None,
        }
        # Offline, the last values the node reported at pairing / its last hello.
        session = runtime.listener.sessions.get(device.id) if runtime.listener else None
        if session is None and row is not None:
            extra["os"] = row.os or None
            extra["version"] = row.version or None
        return extra

    async def h_narrow(ctx: RequestContext, p: NarrowRequest) -> dict[str, Any]:
        device = pairing.get(p.node_id)
        if device is None or device.revoked:
            raise IpcError(ERR_NOT_FOUND, "no such node", details={"reason": "no_node"})
        if p.group not in GROUPS:
            raise IpcError(ERR_VALIDATION, "unknown capability group", details={"reason": "group"})
        current = policy()
        if not p.narrowed and not current.enabled_on_device(p.node_id, p.group):
            # Widening beyond the device is impossible from here, by design.
            raise IpcError(
                ERR_VALIDATION,
                "only the device itself can enable this group",
                details={"reason": "enable_on_device"},
            )
        # Taking away is always allowed on this machine; giving back is administration.
        await authorize_admin(core, ctx, p.pin, "node.narrow", local_needs_pin=not p.narrowed)
        registry = runtime.registry
        if registry is not None:
            await registry.set_narrowed(p.node_id, p.group, p.narrowed)
        else:
            await asyncio.to_thread(current.set_narrowed, p.node_id, p.group, p.narrowed)
        core.security.audit.append(
            actor=ctx.client_id or ctx.role,
            tool="node",
            action="node.narrow",
            target=f"device:{p.node_id} {p.group}",
            decision="allow",
            result="narrowed" if p.narrowed else "restored",
        )
        return {
            "ok": True,
            "node_id": p.node_id,
            "groups": current.view(p.node_id, relevant_groups()),
        }

    async def h_defaults_set(ctx: RequestContext, p: DefaultsSetRequest) -> dict[str, Any]:
        if p.group not in relevant_groups():
            raise IpcError(ERR_VALIDATION, "no such tool family", details={"reason": "group"})
        if p.device not in DEFAULT_WORDS:
            device = pairing.get(p.device)
            if device is None or device.revoked:
                raise IpcError(ERR_NOT_FOUND, "no such node", details={"reason": "no_node"})
        await authorize_admin(core, ctx, p.pin, "node.defaults", local_needs_pin=True)
        current = dict(core.config.devices.defaults)
        tools = family_tools(p.group, _routable(), current)
        updated = {**current, **{tool: p.device for tool in tools}}
        section = DevicesConfig(defaults=updated)  # validated before anything is written
        await asyncio.to_thread(_persist, _user_config_path(core), {t: p.device for t in tools})
        # The router reads `core.config.devices.defaults` on every call: the change is live.
        core.config = core.config.model_copy(update={"devices": section})
        core.security.audit.append(
            actor=ctx.client_id or ctx.role,
            tool="node",
            action="node.defaults",
            target=f"{p.group} -> {p.device}",
            decision="allow",
            result="ok",
        )
        listed = await asyncio.to_thread(pairing.nodes)
        return {"ok": True, "defaults": defaults_view(section.defaults, listed)}

    reg = core.registry.register
    reg("node.narrow", NarrowRequest, h_narrow, roles=UI_ROLES)
    reg("node.defaults.set", DefaultsSetRequest, h_defaults_set, roles=UI_ROLES)
    return details


def _user_config_path(core: Any) -> Path:
    path = getattr(core, "user_config_path", None)
    if isinstance(path, Path):
        return path
    from nox.settings.layers import resolve_user_config_path  # noqa: PLC0415

    return resolve_user_config_path()


def _persist(path: Path, values: dict[str, str]) -> None:
    from nox.settings.layers import write_user_config  # noqa: PLC0415

    write_user_config(path, {"devices": {"defaults": values}})


async def send_policy(session: Any, narrowed: set[str]) -> None:
    """Tell a connected node what the core narrowed; it replaces the node's earlier narrowing."""
    try:
        await session.client.request("nox/policy", {"narrow": sorted(narrowed)}, timeout_s=5)
    except (TimeoutError, McpClientError) as exc:
        # The core filters narrowed groups itself, so nothing is routed there either way.
        log.warning("node.policy_failed", device_id=session.device_id, error=str(exc))
