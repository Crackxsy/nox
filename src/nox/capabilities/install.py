"""`install(core)`: wires the capability catalogue onto a started core.

Returns `None`: there is nothing to shut down. Nothing is cached either - every caller gets a
report built from the state the system is in at that moment, because the honest answer to "what can
you do?" changes with the profile, the mode, the privacy state and which plugins are up.

Two things this module knows that the catalogue cannot:

* which plugin registered which tool, and which plugins are installed but off - `PluginManager`
  already carries both in its status list,
* that a registered tool can be useless for want of a credential. `home.*` exists as soon as the
  plugin runs, and does nothing at all until a token is stored. Without this, the model would read
  `home.light` in its catalogue, promise to dim the lights, and fail in front of the user.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from nox.ai.tooluse import OfferedTool
from nox.capabilities.catalog import DEFAULT_AGENT, build_report
from nox.capabilities.ipc import register_capability_ipc
from nox.capabilities.model import CapabilityReport, CapabilityState
from nox.capabilities.tools import register_capability_tools
from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.core.toolloop import ToolGate
from nox.home.install import ACCESS_TOKEN_SECRET as HOME_TOKEN_SECRET
from nox.ipc.dispatch import RequestRegistry
from nox.plugins.manager import PluginState
from nox.security.model import Decision
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["install"]

DEFAULT_MODE = "companion"

#: Tool prefix -> (secret name, what to tell the user). The one place where "the tool is there and
#: has no credential" is written down; the secret *name* is imported from the module that owns it,
#: so renaming it there cannot leave a stale copy here.
_SECRET_PREREQUISITES: tuple[tuple[str, str, str], ...] = (
    (
        "home.",
        HOME_TOKEN_SECRET,
        "no Home Assistant token is stored - add it on the Settings page",
    ),
)


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    state: Any
    security: Any
    registry: RequestRegistry
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    orchestrator: Any
    plugins: Any


def _mode(core: CoreLike) -> str:
    """Current assistant mode, defaulting to `companion` so a report is never left modeless."""
    try:
        value = core.state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a description
        log.warning("capabilities.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
        return DEFAULT_MODE
    return str(value) if value else DEFAULT_MODE


def _has_secret(core: CoreLike, name: str) -> bool:
    try:
        return core.security.secrets.get(name) is not None
    except Exception as exc:  # noqa: BLE001 - an unavailable keyring is "no secret"
        log.warning("capabilities.secret_read_failed", error=type(exc).__name__)
        return False


def _plugin_status(core: CoreLike) -> list[Any]:
    manager = getattr(core, "plugins", None)
    if manager is None:
        return []
    try:
        return list(manager.status())
    except Exception as exc:  # noqa: BLE001 - a broken manager must not break the catalogue
        log.warning("capabilities.plugin_status_failed", error=f"{type(exc).__name__}: {exc}")
        return []


def install(core: CoreLike) -> ExtensionRuntime:
    def probe(tool_name: str) -> tuple[CapabilityState, str] | None:
        for prefix, secret, detail in _SECRET_PREREQUISITES:
            if tool_name.startswith(prefix) and not _has_secret(core, secret):
                return CapabilityState.UNAVAILABLE, detail
        return None

    def report() -> CapabilityReport:
        statuses = _plugin_status(core)
        sources = {tool: status.plugin_id for status in statuses for tool in status.tools}
        #: Enabled but not running is its own answer. The tools are simply absent from the registry
        #: in that state, so without this row the capability would vanish without explanation.
        stalled: Iterable[tuple[str, str]] = [
            (
                f"{status.plugin_id}.*",
                f"the plugin is enabled but {status.state}"
                + (f": {status.reason}" if status.reason else ""),
            )
            for status in statuses
            if status.enabled and status.state is not PluginState.RUNNING
        ]
        return build_report(
            tools=core.tool_registry,
            engine=core.security.engine,
            mode=_mode(core),
            privacy_mode=core.security.privacy.mode.value,
            sources=sources,
            probe=probe,
            installed_plugins=[status.plugin_id for status in statuses],
            enabled_plugins=[status.plugin_id for status in statuses if status.enabled],
            missing_prerequisites=stalled,
        )

    def offer() -> list[OfferedTool]:
        """The tools the model is told about: what the catalogue says is usable right now.

        Built from the report rather than from the registry, so a tool the active profile denies is
        never mentioned - cheaper than offering it and refusing the call, and more honest. One that
        would ask the user first *is* offered and marked, because hiding it would take the decision
        away from the person the dialog is for.
        """
        described = core.tool_registry.describe()
        schemas = {tool.name: tool.input_schema for tool in described}
        return [
            OfferedTool(
                name=capability.name,
                description=capability.description,
                schema=schemas.get(capability.name, {}),
                asks_first=capability.decision is Decision.CONFIRM,
            )
            for capability in report().capabilities
            if capability.state is CapabilityState.AVAILABLE
            and capability.decision is not Decision.DENY
        ]

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        """Every tool a conversation reaches goes through the same executor as a dashboard click.

        The agent name matches the one the report was built with on purpose: if the offer were made
        for one agent and the call carried out as another, the model would be told it may do things
        that are then refused - which is exactly how the preset runner broke.
        """
        return await core.tool_executor.call(
            agent=DEFAULT_AGENT, name=name, arguments=arguments, mode=_mode(core)
        )

    register_capability_tools(core.tool_registry, report)
    register_capability_ipc(core.registry, report)

    orchestrator = getattr(core, "orchestrator", None)
    if orchestrator is not None:
        orchestrator.tool_gate = ToolGate(offer=offer, call=call)
    else:
        log.warning("capabilities.no_orchestrator", detail="conversations will have no tools")
    log.info("capabilities.installed")
    return None
