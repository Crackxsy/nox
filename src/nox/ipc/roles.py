"""Which client roles count as a person's own user interface, and which of them may administer.

One definition, imported by every request surface the dashboard uses, so a new UI role cannot be
granted in one module and forgotten in the next.

Two tiers, because a paired device is not the machine Nox runs on. A phone can be lost in the
home network, so it gets what a person uses day to day - talking, the day, the house, plans,
approvals, the kill switch - but nothing that changes how Nox is set up or what it may do later:
settings, secrets, the PIN-free relaxing of privacy, resuming after the kill switch, editing
memory, extensions, pairing. Those stay with the shell and the dashboard on this machine
(`LOCAL_UI_ROLES`). `remote` (the Telegram companion) and `pet` are no UI roles at all; they have
their own, much narrower surfaces.
"""

from __future__ import annotations

__all__ = ["CONFIRMING_ROLES", "DEVICE_ROLE", "LOCAL_UI_ROLES", "UI_ROLES"]

DEVICE_ROLE = "device"

#: The desktop shell and the dashboard on this machine: everything, administration included.
LOCAL_UI_ROLES: tuple[str, ...] = ("shell", "dashboard")

#: Day-to-day surfaces: the local UIs and a paired device in the home network.
UI_ROLES: tuple[str, ...] = (*LOCAL_UI_ROLES, DEVICE_ROLE)

#: Who may answer a permission prompt: everyone a person looks at. The first answer wins
#: (`DefaultPermissionEngine.reply`) and every client sees the outcome. A device's answer is never
#: remembered as a lasting grant (`CoreHandlers.permission_reply`).
CONFIRMING_ROLES: tuple[str, ...] = UI_ROLES
