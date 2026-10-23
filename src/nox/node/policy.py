"""What the dashboard may change about a node: narrow its capability groups, and say which device a
tool family runs on by default (spec C §1.10).

The device decides what Nox may do on it (`node.yaml`, `nox node allow`). From the core the owner
can only *take away* - a narrowed group is filtered out of what the core routes and sent to the
node as `nox/policy`, so both sides refuse it - and clear that narrowing again. Clearing can never
widen beyond the device: the node's own gate only ever offers what it enabled itself. A group that
is neither narrowed here nor enabled on the device cannot be switched on from here at all: the
answer is `enable_on_device`, and the page says how to do it on the device itself.

What a device enabled comes from the device: a node says it outright with every tool list
(`_nox.groups`, `nox.node.agent`). An older node does not, and then it is inferred from the tools
it lists - complete right after connecting (a node forgets the core's narrowing with the
connection), partial later (a narrowed group is no longer listed, so its absence says nothing).

Which group a tool belongs to is decided here, by the tool's name, never by the node's word: a node
claiming that `shell.run` is a `system` tool cannot escape a narrowed `shell` group that way, and
a tool of no known group is dropped (fail closed).

Requests, for the UI roles (`nox.ipc.roles`):
  * `node.narrow {node_id, group, narrowed, pin?}` - narrowing needs no PIN on this machine (it
    only takes away; from a paired device the PIN, as for removing a node); clearing it widens
    what Nox may do again, so it needs the PIN when one is set, and from a paired device always.
  * `node.defaults.set {group, device, pin?}` - `device` is `auto`, `core`, `origin` or a paired
    node's id. It decides where a request lands, so it is administration like `node.link`.

Both are audited with their outcome, refusals included. Removing a node resets every default that
pointed at it to `auto`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    "reported_groups",
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
#: Groups that need two keys (C9): enabled on the device *and* switched on for that node on the
#: core by the owner - a voice satellite sends what it hears to Nox as the user's words. The
#: second key is a persisted fact (`node_capabilities.core_opted_in`, migration 0018), set only by
#: clearing the narrowing in the Devices page (`node.narrow`, with the PIN when one is set, from
#: a paired device always) and cleared by narrowing it again or removing the node. Without it the
#: group is narrowed on every listing.
CORE_OPT_IN: frozenset[str] = frozenset({"voice"})
#: "No default": the router decides as if nothing was configured (`nox.node.router.resolve`).
AUTO = "auto"
DEFAULT_WORDS: frozenset[str] = frozenset({AUTO, "core", "origin"})


def group_of(tool: str) -> str | None:
    """The capability group of a tool, by its name; None for a name outside every group."""
    return _PREFIX_GROUP.get(tool.split(".", 1)[0])


def family_tools(group: str, routable: Iterable[str], configured: Iterable[str]) -> list[str]:
    """The tools a family default applies to: those Nox routes, and those already configured."""
    return sorted({t for t in (*routable, *configured) if group_of(t) == group})


def reported_groups(listed: Any) -> set[str] | None:
    """The groups a node says it enabled (`_nox.groups` of its tool list), or None when it does
    not say (an older node). Unknown names are ignored."""
    nox = listed.get("_nox") if isinstance(listed, dict) else None
    groups = nox.get("groups") if isinstance(nox, dict) else None
    if not isinstance(groups, list):
        return None
    return {g for g in groups[:64] if isinstance(g, str) and g in GROUPS}


class NodePolicy:
    """The core's narrowing of each node, kept in `node_capabilities` (migration 0017). Every
    method touches the database: call them off the event loop."""

    def __init__(
        self, nodes: NodeRepository, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._nodes = nodes
        self._clock = clock or (lambda: datetime.now(UTC))

    def narrowed(self, device_id: str) -> set[str]:
        return self._nodes.narrowed_groups(device_id)

    def record_listing(
        self,
        device_id: str,
        tools: Mapping[str, str],
        *,
        reported: set[str] | None = None,
        complete: bool = False,
    ) -> dict[str, str]:
        """A node's accepted tool list: remember which groups the device enabled, and return the
        tools Nox may route - none of a narrowed group, none of an unknown group.

        `reported`: what the node said it enabled (authoritative). Without it, the listed groups;
        a narrowed group missing from the list keeps what was known about it, unless the listing
        is `complete` (the first one after connecting).
        """
        narrowed = self.narrowed(device_id)
        listed = {g for g in (group_of(t) for t in tools) if g is not None}
        if reported is not None:
            enabled = {g: g in reported for g in GROUPS}
        else:
            before = self._nodes.capabilities(device_id)
            enabled = {g: True for g in listed}
            if not complete:
                for group in narrowed - listed:
                    enabled[group] = before.get(group, (False, True))[0]
        # Two keys (C9): a `CORE_OPT_IN` group the owner has not switched on here is narrowed -
        # on every listing, whatever the device says and whenever it says it, the first one
        # included. Only the owner's PIN-protected clearing in the Devices page opts it in.
        opted = self._nodes.opted_in_groups(device_id)
        for group in CORE_OPT_IN - opted:
            if enabled.get(group) and group not in narrowed:
                self._nodes.narrow(device_id, group, True, when=self._clock())
            narrowed.add(group)  # filtered here even if writing the narrowing failed
        self._nodes.set_reported_groups(device_id, enabled, when=self._clock())
        return {
            t: r for t, r in tools.items() if (g := group_of(t)) is not None and g not in narrowed
        }

    def set_narrowed(self, device_id: str, group: str, narrowed: bool) -> None:
        """The owner's switch in the Devices page. For a `CORE_OPT_IN` group, clearing the
        narrowing is the owner's second key and narrowing it again takes the key back."""
        if group in CORE_OPT_IN:
            self._nodes.set_opted_in(device_id, group, not narrowed, when=self._clock())
        self._nodes.narrow(device_id, group, narrowed, when=self._clock())

    def opted_in(self, device_id: str, group: str) -> bool:
        """Whether the owner switched `group` on for this node on the core (`CORE_OPT_IN`)."""
        return group not in CORE_OPT_IN or group in self._nodes.opted_in_groups(device_id)

    def state(self, device_id: str, group: str) -> tuple[bool, bool]:
        """(enabled on the device, narrowed here)."""
        return self._nodes.capabilities(device_id).get(group, (False, False))

    def view(self, device_id: str, relevant: Iterable[str]) -> list[dict[str, Any]]:
        """Every group worth showing for one node, in the order of `GROUPS`: those Nox can route
        a tool of, and any the core narrowed (so it can be cleared again)."""
        caps = self._nodes.capabilities(device_id)
        shown = set(relevant) | {g for g, (_on, narrow) in caps.items() if narrow}
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
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=64)
    group: str = Field(min_length=1, max_length=32)
    narrowed: bool
    pin: str | None = Field(default=None, max_length=128)


class DefaultsSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def _points_at(value: str, device: DeviceRow) -> bool:
    return value == device.id or value.casefold() == device.name.casefold()


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
            found = [d for d in active if _points_at(raw, d)]
            device = found[0].id if len(found) == 1 else raw
        out.append({"group": group, "tools": tools, "device": device, "raw": raw})
    return out


def register_policy_requests(
    core: Any, runtime: Any
) -> Callable[[list[DeviceRow], set[str]], dict[str, dict[str, Any]]]:
    """Register `node.narrow` and `node.defaults.set`; returns what `node.list` adds per node
    (blocking: call it in a thread)."""
    pairing = runtime.pairing
    nodes = NodeRepository(core.db)
    standalone = NodePolicy(nodes)
    #: One writer of `devices.defaults` at a time (the dashboard, a removal).
    defaults_lock = asyncio.Lock()

    def policy() -> NodePolicy:
        registry = runtime.registry
        return registry.policy if registry is not None else standalone

    def details(listed: list[DeviceRow], online: set[str]) -> dict[str, dict[str, Any]]:
        relevant = relevant_groups()
        out: dict[str, dict[str, Any]] = {}
        for device in listed:
            row = nodes.get_node(device.id)
            extra: dict[str, Any] = {
                "groups": policy().view(device.id, relevant),
                "arch": row.arch if row is not None else None,
            }
            # Offline: the last values the node reported (at pairing, at its last hello).
            if device.id not in online and row is not None:
                extra["os"] = row.os or None
                extra["version"] = row.version or None
            out[device.id] = extra
        return out

    def audit(ctx: RequestContext, action: str, target: str, decision: str, result: str) -> None:
        core.security.audit.append(
            actor=ctx.client_id or ctx.role,
            tool="node",
            action=action,
            target=target,
            decision=decision,
            result=result,
        )

    def refuse(
        ctx: RequestContext, action: str, target: str, code: str, message: str, reason: str
    ) -> IpcError:
        audit(ctx, action, target, "deny", reason)
        return IpcError(code, message, details={"reason": reason})

    background: set[asyncio.Future[Any]] = set()

    def finished(task: asyncio.Future[Any]) -> None:
        background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("node.policy_apply_failed", error=type(task.exception()).__name__)

    async def shielded(coro: Any) -> Any:
        """Run `coro` to its end even when the caller is cancelled (a dashboard that disconnects
        cancels its request): a change half applied would leave the database and the routing
        disagreeing. The task audits its own outcome."""
        task = asyncio.ensure_future(coro)
        background.add(task)
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def h_narrow(ctx: RequestContext, p: NarrowRequest) -> dict[str, Any]:
        # Authorization first: a refusal is audited only for someone allowed to ask at all, so a
        # paired device without the PIN cannot fill the audit trail.
        await authorize_admin(core, ctx, p.pin, "node.narrow", local_needs_pin=not p.narrowed)
        target = f"device:{p.node_id} {p.group}"
        device = pairing.get(p.node_id)
        if device is None or device.revoked:
            raise refuse(ctx, "node.narrow", target, ERR_NOT_FOUND, "no such node", "no_node")
        if p.group not in GROUPS:
            raise refuse(
                ctx, "node.narrow", target, ERR_VALIDATION, "unknown capability group", "group"
            )
        current = policy()
        on_device, narrowed_here = await asyncio.to_thread(current.state, p.node_id, p.group)
        if not p.narrowed and not narrowed_here and not on_device:
            # Nothing to clear here, and widening beyond the device is impossible from here.
            raise refuse(
                ctx,
                "node.narrow",
                target,
                ERR_VALIDATION,
                "only the device itself can enable this group",
                "enable_on_device",
            )
        result = "narrowed" if p.narrowed else "cleared"

        async def apply() -> None:
            try:
                registry = runtime.registry
                if registry is not None:
                    await registry.set_narrowed(p.node_id, p.group, p.narrowed)
                else:
                    await asyncio.to_thread(current.set_narrowed, p.node_id, p.group, p.narrowed)
            except BaseException:
                audit(ctx, "node.narrow", target, "allow", "failed")
                raise
            audit(ctx, "node.narrow", target, "allow", result)

        try:
            await shielded(apply())
        except asyncio.CancelledError:
            # The caller is gone; the change itself still completes and audits its outcome.
            audit(ctx, "node.narrow", target, "allow", "cancelled")
            raise
        groups = await asyncio.to_thread(current.view, p.node_id, relevant_groups())
        return {"ok": True, "node_id": p.node_id, "groups": groups}

    async def write(values: dict[str, str]) -> dict[str, str]:
        async with defaults_lock:
            live = core.config.devices.defaults
            updated = {**live, **values}
            DevicesConfig(defaults=updated)  # validated before anything is written
            await asyncio.to_thread(_persist, _user_config_path(core), values)
            # The router reads this mapping on every call: changed in place, the change is live.
            live.update(values)
            return dict(live)

    async def write_defaults(values: dict[str, str]) -> dict[str, str]:
        """Persist and apply, to the end: the file and the live mapping never disagree."""
        written: dict[str, str] = await shielded(write(values))
        return written

    async def h_defaults_set(ctx: RequestContext, p: DefaultsSetRequest) -> dict[str, Any]:
        await authorize_admin(core, ctx, p.pin, "node.defaults", local_needs_pin=True)
        target = f"{p.group} -> {p.device}"
        if p.group not in relevant_groups():
            raise refuse(
                ctx, "node.defaults", target, ERR_VALIDATION, "no such tool family", "group"
            )
        if p.device not in DEFAULT_WORDS:
            device = pairing.get(p.device)
            if device is None or device.revoked:
                raise refuse(ctx, "node.defaults", target, ERR_NOT_FOUND, "no such node", "no_node")
        tools = family_tools(p.group, _routable(), core.config.devices.defaults)

        async def apply() -> dict[str, str]:
            try:
                now = await write({tool: p.device for tool in tools})
            except BaseException:
                audit(ctx, "node.defaults", target, "allow", "failed")
                raise
            audit(ctx, "node.defaults", target, "allow", "ok")
            return now

        try:
            now: dict[str, str] = await shielded(apply())
        except asyncio.CancelledError:
            audit(ctx, "node.defaults", target, "allow", "cancelled")
            raise
        listed = await asyncio.to_thread(pairing.nodes)
        return {"ok": True, "defaults": defaults_view(now, listed)}

    async def forget_defaults(device_id: str, _reason: str) -> None:
        # A removed node is no default any more: those tools are back to `auto`.
        device = pairing.get(device_id)
        if device is None:
            return
        stale = {t: AUTO for t, v in core.config.devices.defaults.items() if _points_at(v, device)}
        if not stale:
            return
        try:
            await write_defaults(stale)
        except Exception as exc:  # noqa: BLE001 - the removal itself stands either way
            log.error("node.defaults_reset_failed", device_id=device_id, error=type(exc).__name__)
            return
        core.security.audit.append(
            actor="core",
            tool="node",
            action="node.defaults",
            target=f"device:{device_id} removed",
            decision="allow",
            result="reset:" + ",".join(sorted(stale)),
        )

    pairing.on_revoked(forget_defaults)
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
