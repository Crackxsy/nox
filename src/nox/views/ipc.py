"""The `views.*` requests the Board page calls.

Read and clear, nothing else. A view is drawn by the model through `view.show`; a request that let
the dashboard *create* one would be a second path into the same board with none of the validation
the tool goes through.
"""

from __future__ import annotations

from typing import Any

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.views.board import Board

log = get_logger(__name__)

__all__ = ["register_view_ipc"]

#: The two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")


def register_view_ipc(registry: RequestRegistry, board: Board) -> None:
    """Register `views.list` and `views.clear` for the `shell` and `dashboard` roles."""

    async def v_list(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return board.as_payload()

    async def v_clear(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        removed = board.clear()
        log.info("views.cleared", removed=removed)
        return {"ok": True, "removed": removed}

    registry.register("views.list", EmptyPayload, v_list, roles=UI_ROLES)
    registry.register("views.clear", EmptyPayload, v_clear, roles=UI_ROLES)
