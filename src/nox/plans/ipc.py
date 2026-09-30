"""The `plans.*` requests the dashboard calls.

Approving from the dashboard needs no confirmation dialog, and that is not a shortcut: the click
*is* the confirmation. The `plans.start` tool exists for the other direction - the model asking -
and that one carries high risk precisely because nobody clicked anything.

The steps themselves are checked either way. A plan approved with one click still runs every step
through the permission engine, so a step that needs confirming asks when it gets there.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_NOT_FOUND, IpcError
from nox.plans.book import PlanBook

log = get_logger(__name__)

__all__ = ["register_plan_ipc"]

#: The two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")


class PlanRef(BaseModel):
    plan: str = Field(min_length=1, max_length=40)


def register_plan_ipc(registry: RequestRegistry, book: PlanBook) -> None:
    """Register the three `plans.*` requests for the `shell` and `dashboard` roles."""

    async def p_list(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return {
            "waiting": [plan.review() for plan in book.waiting()],
            "started": book.history(),
        }

    async def p_approve(_ctx: RequestContext, payload: PlanRef) -> dict[str, Any]:
        try:
            row = await book.start(payload.plan)
        except KeyError as exc:
            raise IpcError(
                ERR_NOT_FOUND, f"there is no plan waiting with the id {payload.plan!r}"
            ) from exc
        log.info("plans.approved", plan=payload.plan, task=row.id, by="dashboard")
        return {"ok": True, "plan": payload.plan, "task": row.id}

    async def p_discard(_ctx: RequestContext, payload: PlanRef) -> dict[str, Any]:
        if not book.forget(payload.plan):
            raise IpcError(ERR_NOT_FOUND, f"there is no plan waiting with the id {payload.plan!r}")
        return {"ok": True, "plan": payload.plan}

    registry.register("plans.list", EmptyPayload, p_list, roles=UI_ROLES)
    registry.register("plans.approve", PlanRef, p_approve, roles=UI_ROLES)
    registry.register("plans.discard", PlanRef, p_discard, roles=UI_ROLES)
