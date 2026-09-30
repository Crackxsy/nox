"""The `capabilities.report` request behind the dashboard's control centre.

The dashboard gets the whole report, descriptions and all, because it is drawing a page for the
person who owns the machine. That is the opposite of what the `capabilities.list` *tool* sends: the
model already has the descriptions and needs only the states. Same data, two audiences, and
pretending one payload fits both is what makes such pages either useless or wasteful.

Read-only, like every other honest mirror of state. Nothing here changes a permission - that is
done by switching profile or mode, and those requests already exist.
"""

from __future__ import annotations

from typing import Any

from nox.capabilities.tools import ReportProvider
from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry

log = get_logger(__name__)

__all__ = ["register_capability_ipc"]

#: The two user-facing clients, never a plugin or a worker.
UI_ROLES = ("shell", "dashboard")


def register_capability_ipc(registry: RequestRegistry, report_of: ReportProvider) -> None:
    """Register `capabilities.report` for the `shell` and `dashboard` roles."""

    async def h_report(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return report_of().model_dump(mode="json")

    registry.register("capabilities.report", EmptyPayload, h_report, roles=UI_ROLES)
