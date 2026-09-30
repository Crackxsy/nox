"""The `extend.*` requests the dashboard calls.

Read only. A proposal is started by asking Nox, which goes through the permission engine like
anything else; a request that let the dashboard start one would be a second way in with none of
that. Merging is git's job and the user's.
"""

from __future__ import annotations

from typing import Any

from nox.core.logging import get_logger
from nox.extend.runner import ProposalRunner
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry

log = get_logger(__name__)

__all__ = ["register_extend_ipc"]

#: The two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")


def register_extend_ipc(registry: RequestRegistry, runner: ProposalRunner) -> None:
    """Register `extend.list` for the `shell` and `dashboard` roles."""

    async def e_list(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return {"proposals": [proposal.as_dict() for proposal in runner.all()]}

    registry.register("extend.list", EmptyPayload, e_list, roles=UI_ROLES)
