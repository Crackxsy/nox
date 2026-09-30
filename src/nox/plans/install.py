"""`install(core) -> PlansRuntime`: wires plans onto a started core.

The queue is this extension's own, the way the Rocket League backfill has its own. That is the
established shape here and it earns its keep: a dedicated queue pauses on its own while a game is
running, so a plan never competes with the game for the machine.

Every step is called under the `plans` agent rather than `companion`, for the same reason preset
steps have their own agent name: the core carrying out a list the user approved is a different
situation from a language model asking for a tool mid-sentence, and the profiles have to be able to
tell the two apart. Nothing is granted here - the rules decide, and without a rule a step gets the
ordinary treatment for its risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.logging import get_logger
from nox.core.tasks import TaskQueue
from nox.data.repos import TaskRepository
from nox.ipc.dispatch import RequestRegistry
from nox.plans.book import PlanBook
from nox.plans.ipc import register_plan_ipc
from nox.plans.runner import KIND, plan_handler
from nox.plans.tools import register_plan_tools
from nox.tools.executor import ToolExecutor
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["PlansRuntime", "install"]

#: Steps run as `plans`, so a profile can say yes to work the user approved without saying yes to
#: the same tool asked for directly in a conversation.
_AGENT = "plans"

DEFAULT_MODE = "companion"


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    bus: Any
    db: Any
    state: Any
    registry: RequestRegistry
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor


@dataclass(slots=True)
class PlansRuntime:
    """Handle for the caller: the book other code may read, and the queue to stop."""

    book: PlanBook
    queue: TaskQueue

    async def stop(self) -> None:
        await self.queue.stop()


def _mode(core: CoreLike) -> str:
    """Current assistant mode, defaulting to `companion` so a step is never left modeless."""
    try:
        value = core.state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a step
        log.warning("plans.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
        return DEFAULT_MODE
    return str(value) if value else DEFAULT_MODE


def install(core: CoreLike) -> PlansRuntime:
    tasks = TaskRepository(core.db)
    queue = TaskQueue(core.bus, tasks)

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return await core.tool_executor.call(
            agent=_AGENT, name=name, arguments=arguments, mode=_mode(core)
        )

    queue.register(KIND, plan_handler(call))
    book = PlanBook(queue=queue, tasks=tasks)

    register_plan_tools(core.tool_registry, book)
    register_plan_ipc(core.registry, book)
    queue.start()

    log.info("plans.installed")
    return PlansRuntime(book=book, queue=queue)
