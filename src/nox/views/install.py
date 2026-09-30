"""`install(core) -> ViewsRuntime`: wires the board onto a started core.

The event carries the view's id, kind and title, and not the view itself. A dashboard that is open
hears `view.shown` and re-reads `views.list`, the same way it re-reads a setting after
`settings.changed` - an event payload is a notification, not a transport for a hundred table rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from nox.core.events import E, Event
from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestRegistry
from nox.tools.registry import ToolRegistry
from nox.views.board import Board
from nox.views.ipc import register_view_ipc
from nox.views.model import View
from nox.views.tools import register_view_tools

log = get_logger(__name__)

__all__ = ["ViewsRuntime", "install"]


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    bus: Any
    registry: RequestRegistry
    tool_registry: ToolRegistry


@dataclass(slots=True)
class ViewsRuntime:
    """Handle for the caller: the board other code may read.

    No `stop()`, because there is nothing to stop - no task, no process, no file handle. The
    extension contract allows that (`nox.core.extension`: the core calls `stop()` only if it is
    there), and an empty one would be a method that exists to satisfy a type rather than to do
    anything.
    """

    board: Board


def install(core: CoreLike) -> ViewsRuntime:
    board = Board()

    async def announce(view: View) -> None:
        await core.bus.publish(
            Event(
                name=E.VIEW_SHOWN,
                payload={"id": view.id, "kind": view.body.kind, "title": view.body.title},
                source="views",
            )
        )

    register_view_tools(core.tool_registry, board, announce)
    register_view_ipc(core.registry, board)
    log.info("views.installed")
    return ViewsRuntime(board=board)
