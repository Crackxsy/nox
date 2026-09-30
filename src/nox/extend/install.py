"""`install(core) -> ExtendRuntime`: wires self-extension onto a started core.

Registered even when no workspace is configured, which is the default. A model that has
`extend.propose` and gets "name a checkout in extend.workspace" can tell the user something useful;
a model with no such tool can only say it cannot change itself, which is the wrong answer to "can
you add that yourself?".

Proposals run under their own agent name for the same reason preset steps do: the core carrying out
a change the user asked for and confirmed is a different situation from a language model reaching
for the same tool mid-sentence, and a profile has to be able to tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.logging import get_logger
from nox.extend.ipc import register_extend_ipc
from nox.extend.runner import ProposalRunner
from nox.extend.tools import register_extend_tools
from nox.ipc.dispatch import RequestRegistry
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["ExtendRuntime", "install"]

_AGENT = "extend"
DEFAULT_MODE = "coding"


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    state: Any
    registry: RequestRegistry
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor


@dataclass(slots=True)
class ExtendRuntime:
    """Handle for the caller: the runner other code may read. Nothing to stop."""

    runner: ProposalRunner


def _mode(core: CoreLike) -> str:
    """Current assistant mode. `coding` is the default here because that is the only profile the
    plugin that writes the change runs under at all."""
    try:
        value = core.state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a proposal
        log.warning("extend.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
        return DEFAULT_MODE
    return str(value) if value else DEFAULT_MODE


def install(core: CoreLike) -> ExtendRuntime:
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        return await core.tool_executor.call(
            agent=_AGENT, name=name, arguments=arguments, mode=_mode(core)
        )

    runner = ProposalRunner(
        settings=lambda: core.config.extend,
        call_tool=call_tool,
        tool_known=lambda name: name in core.tool_registry,
    )
    register_extend_tools(core.tool_registry, runner)
    register_extend_ipc(core.registry, runner)
    log.info(
        "extend.installed",
        workspace=bool(core.config.extend.workspace),
        tests=bool(core.config.extend.test_command),
    )
    return ExtendRuntime(runner=runner)
