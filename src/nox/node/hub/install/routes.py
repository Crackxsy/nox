"""The tools a paired PC can be asked to run, routed - and how a call is shown before it is allowed.

`install_node_routes` turns `system.info`, `notify.show`, the hands (`file.*`, `shell.run`), the
world tools, `voice.say` and `obs.request` into routable tools (`nox.node.hub.router`), tells the
capability report which of them only nodes have, and adds the device line to every turn's context.
The renderers in this module are what the person reads in the confirmation and the audit for a call
on a device: in full or not at all, quoted like the device's own dialog. A call that cannot be
shown in full is refused, never cut.

Owns the target renderers and prechecks of the hands and the routing of the tool set. Does not own
how a call is routed (`nox.node.hub.router`) or what a device does with it (`nox.node.device`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nox.capabilities.model import CapabilityState
from nox.node.device.hands import (
    MoveArgs,
    PathArgs,
    ShellArgs,
    WriteArgs,
    delete_refusal,
    move_refusal,
    write_risk,
    write_summary,
    write_text_refusal,
)
from nox.node.hub.install.control import build_world
from nox.node.hub.listener import NodeListener
from nox.node.hub.ports import HOOK_NAME, CoreServices
from nox.node.hub.registry import NodeRegistry
from nox.node.hub.router import CannotShow, core_only_spec, install_routes, routed_spec
from nox.node.hub.satellites import obs_route, voice_say_spec
from nox.node.hub.world_routes import NodeControl
from nox.node.wire.arguments import NoArgs, NotifyArgs
from nox.node.wire.protocol import render
from nox.node.wire.sysinfo import system_info
from nox.security.model import Risk
from nox.security.taint import current_conversation
from nox.tools.registry import ToolSpec

__all__ = [
    "MAX_TARGET_CHARS",
    "core_system_info_spec",
    "delete_precheck",
    "install_node_routes",
    "move_precheck",
    "move_target",
    "shell_target",
    "write_precheck",
    "write_risk_of",
    "write_target",
]

#: Longest permission target a node call may have. Longer is refused, never cut: a target the
#: person cannot read in full is a target that can hide its dangerous tail (padding).
MAX_TARGET_CHARS = 1000

_Target = Callable[[dict[str, Any]], str]


def _complete(text: str) -> str:
    if len(text) > MAX_TARGET_CHARS:
        raise CannotShow(f"the call is too long to be shown in full ({len(text)} characters)")
    return text


def shell_target(payload: dict[str, Any]) -> str:
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


def write_target(payload: dict[str, Any]) -> str:
    """A node write: where, how (new file / overwrite / append), how much, how it starts. A
    write whose text the node would refuse anyway is refused before this - in the route's
    `prepare`, off the event loop (`write_precheck`)."""
    shown = write_summary(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
        overwrite=bool(payload.get("overwrite")),
    )
    return _complete(" ".join(f"{key} {render(value)}" for key, value in shown.items()))


def write_precheck(payload: dict[str, Any]) -> str | None:
    return write_text_refusal(
        str(payload.get("path") or ""),
        str(payload.get("text") or ""),
        append=bool(payload.get("append")),
    )


def write_risk_of(payload: dict[str, Any]) -> Risk:
    path = str(payload.get("path") or "")
    return Risk(write_risk(path, bool(payload.get("append")), bool(payload.get("overwrite"))))


def move_target(payload: dict[str, Any]) -> str:
    """A node move: from where to where - both in full, or not at all."""
    source, to = str(payload.get("path") or ""), str(payload.get("to") or "")
    return _complete(f"move {render(source)} to {render(to)} replaces never")


def move_precheck(payload: dict[str, Any]) -> str | None:
    return move_refusal(
        str(payload.get("path") or ""),
        str(payload.get("to") or ""),
        overwrite=bool(payload.get("overwrite")),
        create_folders=bool(payload.get("create_folders")),
    )


def delete_precheck(payload: dict[str, Any]) -> str | None:
    return delete_refusal(str(payload.get("path") or ""))


#: Node calls whose answer is never remembered: each one is asked on the device again.
_NEVER_REMEMBERED = frozenset({"file.write", "file.move", "file.delete"})
_PRECHECKS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "file.write": write_precheck,
    "file.move": move_precheck,
    "file.delete": delete_precheck,
}


def _field(name: str) -> _Target:
    return lambda payload: _complete(render(str(payload.get(name) or "")))


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


def _hand_specs(
    own: Callable[[str], ToolSpec | None], registry: NodeRegistry, defaults: Callable[[], Any]
) -> list[ToolSpec]:
    """`system.info`, `notify.show` and the hands (`file.*`, `shell.run`), each the core's own
    tool when the call stays here."""
    hands: list[tuple[str, str, Risk, bool, type[BaseModel], _Target]] = [
        ("file.list", "What is in one folder", Risk.READ, False, PathArgs, _field("path")),
        ("file.read", "The text of one file", Risk.READ, False, PathArgs, _field("path")),
        ("file.write", "Write text to a file", Risk.MEDIUM, True, WriteArgs, write_target),
        # On a device both are high: asked there every time, never remembered.
        ("file.move", "Move or rename a file", Risk.HIGH, True, MoveArgs, move_target),
        ("file.delete", "Move a file to the trash", Risk.HIGH, True, PathArgs, _field("path")),
        ("shell.run", "Run a command sandboxed", Risk.HIGH, True, ShellArgs, shell_target),
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
                node_risk=write_risk_of if name == "file.write" else None,
                remember_on_node=name not in _NEVER_REMEMBERED,
                precheck=_PRECHECKS.get(name),
            )
        )
    return specs


def install_node_routes(
    services: CoreServices, registry: NodeRegistry, listener: NodeListener | None
) -> NodeControl:
    """`system.info` (the core has it too), `notify.show` (nodes only) and the hands - `file.*`
    and `shell.run`, each the core's own tool when the call stays here - become routable; the
    capability report hides a node-only tool while no node offers it, and every turn's context
    names the devices. `package.*` stays on the core: it cannot work in a node's sandbox yet, and
    a call that names a paired device is refused (`core_only_spec`). Returns the computer-use
    lock the world tools share. `listener` is None where there is none: a harness with simulated
    devices."""
    tools = services.tool_registry

    def defaults() -> dict[str, str]:
        return services.config.devices.defaults  # read per call: the settings can change

    own = tools.get
    specs = _hand_specs(own, registry, defaults)
    world, control = build_world(services, registry, defaults, listener)
    specs += world

    def tainted_now() -> bool:
        try:
            return bool(services.security.engine.is_tainted(current_conversation()))
        except Exception:  # noqa: BLE001 - cannot tell: tainted
            return True

    specs.append(voice_say_spec(registry, defaults, tainted=tainted_now))  # speak on a node
    for name in ("package.install", "package.remove"):
        mine = own(name)
        if mine is not None:
            # This core only: a call that names a paired device is refused, never run here.
            specs.append(core_only_spec(mine, registry))
    on_core = {spec.name for spec in specs if own(spec.name) is not None}
    install_routes(tools, specs)
    node_only = {spec.name for spec in specs} - on_core
    # `obs.request` - the plugin's tool while OBS runs here, routed to the node where OBS
    # runs otherwise. The plugin comes and goes; the route stays (`ToolRegistry.wrap`).
    tools.wrap("obs.request", obs_route(registry, defaults))

    def probe(tool_name: str) -> tuple[CapabilityState, str] | None:
        node_only_now = tool_name in node_only or (
            tool_name == "obs.request" and tools.own(tool_name) is None
        )
        if node_only_now and not registry.any_online_with(tool_name):
            return CapabilityState.UNAVAILABLE, "no paired device with this is online"
        return None

    tools.add_availability_probe(HOOK_NAME, probe)

    def device_line() -> str:
        return registry.prompt_line()

    services.orchestrator.add_fact_source(HOOK_NAME, device_line)
    return control
