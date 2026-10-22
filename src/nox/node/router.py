"""Routing a tool call to a device: the core itself or one of its nodes (spec C §1.5).

A routable tool is still *one* tool for the model (the offer budget stays as it is), with an
optional `device` argument: "core" or a node's name. When it is left out, in order:

1. what the user said, resolved by the model into `device`;
2. the per-tool default in `devices.defaults` (`core`, `origin` - the node linked to the browser
   device the request came from - or a device name);
3. exactly one device (the core counts) has the tool -> that one;
4. a READ tool may fall back to the core; a tool with side effects **never guesses**: it answers
   `needs_device` with the candidates, and the model asks.

The device is resolved once, in the call's permission scope, and written into the call: the
permission request names it (`device:<id> <target>`), the audit carries it, a remembered grant
is bound to it - and the handler runs exactly there. If that device went offline in between, the
call fails fast with `device_offline`; nothing is queued and no other device is substituted.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, create_model

from nox.core.logging import get_logger
from nox.node.registry import (
    RISKS,
    SESSION_RESULT_KEY,
    NodeEntry,
    NodeRegistry,
    offline_error,
)
from nox.security.model import Risk
from nox.security.taint import current_conversation
from nox.tools.registry import CallScope, ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = [
    "CORE",
    "CannotShow",
    "DEVICE_KEY",
    "Resolution",
    "origin_device",
    "resolve",
    "routed_spec",
    "install_routes",
]

CORE = "core"
DEVICE_KEY = "device"
#: Where a refused resolution travels from the scope to the handler, inside the call's payload.
ROUTE_ERROR_KEY = "_route_error"
#: Risk and delegation as decided in the permission scope - computed once, so the handler and the
#: node act on exactly what the executor decided on (no second look at changing registry state).
ROUTE_RISK_KEY = "_route_risk"
ROUTE_DELEGATE_KEY = "_route_delegate"
#: How long a node call may take before the core gives up on it.
NODE_CALL_TIMEOUT_S = 30.0
_CORE_WORDS = frozenset({"core", "dieser core", "this core", "hier", "here"})
_HERE_WORDS = frozenset({"hier", "here"})


#: `around(entry, arguments, call)`: `call(blob=...)` runs the call on the node.
NodeCall = Callable[..., Awaitable[dict[str, Any]]]
NodeAround = Callable[[NodeEntry, dict[str, Any], NodeCall], Awaitable[dict[str, Any]]]


class CannotShow(ValueError):  # noqa: N818 - a refusal, named for what it says
    """A node call's target would not fit a confirmation in full; the call is refused."""


@dataclass(frozen=True)
class Resolution:
    #: "core", a node's device id, or "" when the call cannot run anywhere.
    device: str
    error: dict[str, Any] | None = None


def origin_device() -> str | None:
    """The paired browser device the current turn came from, if any: its conversation is
    `chat:device:<id>#...` (`nox.core.orchestrator.conversation_for`)."""
    conversation = current_conversation()
    prefix = "chat:device:"
    if not conversation.startswith(prefix):
        return None
    device = conversation[len(prefix) :].split("#", 1)[0]
    return device or None


def _matches(entries: list[NodeEntry], wanted: str) -> list[NodeEntry]:
    key = wanted.strip().casefold()
    return [e for e in entries if key in (e.device_id, e.name.casefold(), e.label.casefold())]


def _match(entries: list[NodeEntry], wanted: str) -> NodeEntry | None:
    found = _matches(entries, wanted)
    return found[0] if len(found) == 1 else None


def resolve(
    tool: str,
    requested: str | None,
    *,
    entries: list[NodeEntry],
    core_has: bool,
    side_effects: bool,
    default: str | None,
    origin: str | None,
) -> Resolution:
    """Pure: the device for one call. See the module docstring for the order."""
    online = [e for e in entries if e.offers(tool)]
    candidates = (["core"] if core_has else []) + [e.label for e in online]

    def needs(reason: str = "needs_device") -> Resolution:
        return Resolution("", {"error": reason, "tool": tool, "candidates": candidates})

    def at(entry: NodeEntry) -> Resolution:
        if entry.offers(tool):
            return Resolution(entry.device_id)
        if not entry.online or entry.connecting:
            return Resolution("", offline_error(entry))
        if not entry.compatible:
            return Resolution("", {"error": "update_needed", "device": entry.label})
        if entry.narrowed_out(tool):
            # Switched off in the dashboard, not on the device: it can be switched on there again.
            return Resolution(
                "",
                {
                    "error": "narrowed_on_core",
                    "device": entry.label,
                    "tool": tool,
                    "hint": "im Dashboard ausgeschaltet – dort wieder einschaltbar",
                },
            )
        # Online, but this device does not offer the tool: only the device itself can change that.
        return Resolution(
            "",
            {
                "error": "not_enabled_on_device",
                "device": entry.label,
                "tool": tool,
                "hint": "nur am Gerät freischaltbar (nox node allow)",
            },
        )

    # (1) the user's (the model's) choice
    if requested is not None and requested.strip():
        word = requested.strip().casefold()
        if word in _HERE_WORDS and origin is not None:
            # "hier" from a browser on a PC with a node means that PC (review N5).
            linked = [e for e in entries if e.linked_device_id == origin]
            if len(linked) == 1:
                return at(linked[0])
        if word in _CORE_WORDS:
            return Resolution(CORE) if core_has else needs("not_on_core")
        found = _matches(entries, requested)
        if len(found) > 1:
            # Two devices answer to that name: never pick one of them.
            return needs("ambiguous_device")
        if not found:
            return needs("unknown_device")
        return at(found[0])
    # (2) the configured default
    if default:
        if default == CORE and core_has:
            return Resolution(CORE)
        if default == "origin":
            # The PC the user is at: its node, online or not - never another one instead.
            linked = [e for e in entries if origin is not None and e.linked_device_id == origin]
            if len(linked) == 1:
                return at(linked[0])
            if side_effects and candidates:
                # Not linked (or not asked from a browser): where the user is, is unknown.
                return needs()
        elif default not in (CORE, "origin", "auto"):  # `auto`: as if nothing was configured
            entry = _match(entries, default)
            if entry is not None:
                # The configured device, or why not - never quietly another one.
                return at(entry)
    # (3) exactly one device has it
    if len(candidates) == 1:
        return Resolution(CORE) if core_has else Resolution(online[0].device_id)
    if not candidates:
        return needs("no_device")
    # (4) a read may stay here; a side effect is never guessed onto a device
    if not side_effects and core_has:
        return Resolution(CORE)
    return needs()


def _routed_model(base: type[BaseModel] | None, tool: str) -> type[BaseModel]:
    field = (
        str | None,
        Field(
            default=None,
            max_length=64,
            description="Which device: 'core' or a device name from the 'Geräte' line. "
            "Leave it out only when the user did not say.",
        ),
    )
    if base is None:
        from pydantic import ConfigDict  # noqa: PLC0415

        return create_model(
            f"Routed_{tool.replace('.', '_')}",
            __config__=ConfigDict(extra="forbid"),
            device=field,
        )
    return create_model(
        f"Routed_{base.__name__}",
        __base__=base,
        device=field,
    )


def _max_risk(a: Risk, b: str) -> Risk:
    order = list(RISKS)
    other = Risk(b) if b in order else Risk.HIGH
    return a if order.index(a.value) >= order.index(other.value) else other


def routed_spec(
    *,
    name: str,
    description: str,
    risk: Risk,
    side_effects: bool,
    registry: NodeRegistry,
    core: ToolSpec | None = None,
    input_model: type[BaseModel] | None = None,
    defaults: Callable[[], Mapping[str, str]] = dict,
    rememberable: bool = True,
    node_target: Callable[[dict[str, Any]], str] | None = None,
    network: bool | Callable[[dict[str, Any]], bool] | None = None,
    node_risk: Callable[[dict[str, Any]], Risk] | None = None,
    remember_on_node: bool = True,
    around: NodeAround | None = None,
) -> ToolSpec:
    """One routable tool: the core's own spec (if the core has the tool) plus every node that
    offers it. `input_model` describes the arguments when the core has no spec of its own.

    `node_target` names what a call on a node does, for the confirmation and the audit - the
    core's own `targets` may describe this machine (`shell.run` resolves programs here), so it
    is used for a node only when no `node_target` is given. A `node_target` that cannot show the
    call in full raises `CannotShow`, and the call is refused (`cannot_show_in_full`).
    `node_risk` raises a node call's risk from its arguments (a write that replaces a file);
    `remember_on_node=False` never lets a "remember" answer cover a call on a node.
    `around` wraps the call on a node: it may refuse before the call (a privacy rule of this
    core), and it turns the node's answer into what the model sees (a capture stored here).
    `network` overrides the template's
    (a download is network whether or not the core has the tool itself)."""
    base = core.input_model if core is not None else input_model
    model = _routed_model(base, name)

    def inner(payload: dict[str, Any]) -> dict[str, Any]:
        hidden = (DEVICE_KEY, ROUTE_ERROR_KEY, ROUTE_RISK_KEY, ROUTE_DELEGATE_KEY)
        return {k: v for k, v in payload.items() if k not in hidden}

    def scope(payload: dict[str, Any]) -> CallScope:
        resolution = resolve(
            name,
            payload.get(DEVICE_KEY),
            entries=registry.entries(),
            core_has=core is not None,
            side_effects=side_effects,
            default=defaults().get(name),
            origin=origin_device(),
        )
        _tool, _, action = name.partition(".")
        if resolution.error is not None:
            # Nothing will run: no confirmation for a call that only reports why not.
            payload[DEVICE_KEY] = ""
            payload[ROUTE_ERROR_KEY] = resolution.error
            return CallScope(action=action, risk=Risk.READ, target="device:none")
        payload[DEVICE_KEY] = resolution.device
        if resolution.device == CORE:
            assert core is not None
            if core.scope is not None:
                return core.scope(inner(payload))
            target = core.targets(inner(payload)) if core.targets is not None else ""
            return CallScope(action=action, risk=core.risk, target=target)
        entry = next(e for e in registry.entries() if e.device_id == resolution.device)
        node_risk_ = _max_risk(risk, entry.tools.get(name, "high"))
        if node_risk is not None:
            node_risk_ = _max_risk(node_risk_, node_risk(inner(payload)).value)
        if node_target is not None:
            try:
                base_target = node_target(inner(payload))
            except CannotShow as exc:
                # Never a shortened target: nothing runs, nobody is asked about half a call.
                payload[DEVICE_KEY] = ""
                payload[ROUTE_ERROR_KEY] = {
                    "error": "cannot_show_in_full",
                    "tool": name,
                    "detail": str(exc)[:200],
                }
                return CallScope(action=action, risk=Risk.READ, target="device:none")
        else:
            base_target = core.targets(inner(payload)) if core is not None and core.targets else ""
        # Left to the device only when it can ask; otherwise the core asks itself (and the
        # node still refuses high risk it cannot confirm, unless trusted there).
        delegate = node_risk_ is Risk.HIGH and entry.confirmer
        payload[ROUTE_RISK_KEY] = node_risk_.value
        payload[ROUTE_DELEGATE_KEY] = delegate
        # The device travels in its own field: the permission request, the audit and a
        # remembered grant carry it; the target stays what the tool says (shown with the name).
        return CallScope(
            action=action,
            risk=node_risk_,
            target=f"{entry.label}: {base_target or name}",
            device=resolution.device,
            confirm_on_device=delegate,
            rememberable=remember_on_node,
        )

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        error = payload.get(ROUTE_ERROR_KEY)
        if isinstance(error, dict):
            return error
        device = payload.get(DEVICE_KEY)
        if device == CORE:
            assert core is not None
            return await core.handler(inner(payload))
        entry = next((e for e in registry.entries() if e.device_id == device), None)
        if entry is None:
            return {"error": "unknown_device", "tool": name}
        # Exactly what the scope decided - the registry may have changed since (a node
        # reconnected without its dialog, reported another risk): never re-decide here.
        decided_risk = payload.get(ROUTE_RISK_KEY)
        if decided_risk not in ("read", "low", "medium", "high"):
            return {"error": "not_routed", "tool": name}
        arguments = inner(payload)

        async def on_node(*, blob: bool = False) -> dict[str, Any]:
            extra: dict[str, Any] = {"blob": True} if blob else {}
            return await registry.call(
                entry,
                name,
                arguments,
                risk=str(decided_risk),
                timeout_s=NODE_CALL_TIMEOUT_S,
                confirm_on_device=bool(payload.get(ROUTE_DELEGATE_KEY)),
                **extra,
            )

        answer = await around(entry, arguments, on_node) if around is not None else await on_node()
        answer.pop(SESSION_RESULT_KEY, None)  # the node's report is for the core, not the model
        return answer

    template = core or ToolSpec(
        name=name,
        description=description,
        input_model=model,
        risk=risk,
        handler=handler,
        side_effects=side_effects,
    )
    return dataclasses.replace(
        template,
        description=description,
        input_model=model,
        handler=handler,
        risk=risk if core is None else core.risk,
        side_effects=side_effects,
        rememberable=rememberable and template.rememberable,
        targets=None,
        scope=scope,
        prepare=lambda _payload: registry.settle(),
        local=False,
        network=template.network if network is None else network,
    )


def install_routes(tools: ToolRegistry, specs: list[ToolSpec]) -> list[str]:
    """Register each spec as routable, replacing the core's own spec of the same name."""
    names: list[str] = []
    for spec in specs:
        if spec.name in tools:
            tools.unregister(spec.name)
        tools.register(spec)
        names.append(spec.name)
    log.info("node.routes", tools=names)
    return names
