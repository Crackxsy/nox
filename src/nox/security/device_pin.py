"""The PIN a paired device must give for what reaches beyond its day-to-day surface.

A paired device (`nox.lan`) may talk, see the day, switch lights and answer Nox's questions. Some of
its requests reach further, and a lost or stolen phone must not be able to make them alone:

  * pairing a new device, and removing another one,
  * approving a plan, which starts work in the background,
  * allowing anything Nox asks about at high or critical risk - otherwise a phone could ask Nox to
    do something through the chat and then allow it itself.

`DevicePinGuard.require` is the one door for all of them. It needs the security PIN, also where the
local dashboard does not, and refuses outright when no PIN is set: without a PIN there is nothing a
device could prove. Wrong PINs are counted per device, and after `limit` of them the device is
revoked - before the PIN's own lockout, which would lock its owner out too, can be reached.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.roles import DEVICE_ROLE
from nox.ipc.tokens import DEVICE_CLIENT_PREFIX
from nox.security.gate import PinRequiredError, SecurityChangeGate

log = get_logger(__name__)

__all__ = ["DEFAULT_PIN_FAILURES", "DevicePinGuard"]

#: Wrong PINs a device may give before it is removed. Below the PIN's own lockout (5).
DEFAULT_PIN_FAILURES = 3


class DevicePinGuard:
    def __init__(
        self,
        gate: Callable[[], SecurityChangeGate],
        revoke: Callable[[str, str], Awaitable[None]],
        *,
        limit: int = DEFAULT_PIN_FAILURES,
    ) -> None:
        """`gate` returns the core's current gate (a test may swap it); `revoke(device_id,
        reason)` removes a device and closes its sessions."""
        self._gate = gate
        self._revoke = revoke
        self._limit = limit
        self._failures: dict[str, int] = {}

    def failures(self, device_id: str) -> int:
        return self._failures.get(device_id, 0)

    async def require(self, ctx: RequestContext, pin: str | None, action: str) -> None:
        """Pass for a local client (its own gate rules apply elsewhere) and for a device that gave
        the right PIN; raise `IpcError(permission.denied)` otherwise."""
        if ctx.role != DEVICE_ROLE:
            return
        device_id = ctx.client_id.removeprefix(DEVICE_CLIENT_PREFIX)
        gate = self._gate()
        if not gate.is_required():
            raise IpcError(
                ERR_PERMISSION,
                f"{action} from a paired device needs the security PIN; set one on the server",
                details={"reason": "pin_not_set"},
            )
        try:
            await gate.require(pin, action=action, by=ctx.client_id)
        except PinRequiredError as exc:
            if pin:
                await self._wrong(device_id, action)
            reason = "pin_wrong" if pin else "pin_required"
            raise IpcError(ERR_PERMISSION, exc.reason, details={"reason": reason}) from exc
        self._failures.pop(device_id, None)

    async def _wrong(self, device_id: str, action: str) -> None:
        count = self._failures.get(device_id, 0) + 1
        self._failures[device_id] = count
        log.warning("security.device_pin_wrong", device=device_id, action=action, count=count)
        if count < self._limit:
            return
        self._failures.pop(device_id, None)
        log.warning("security.device_removed_after_wrong_pins", device=device_id, count=count)
        await self._revoke(device_id, "wrong_pin")
        raise IpcError(
            ERR_PERMISSION,
            f"this device was removed after {count} wrong PINs; pair it again",
            details={"reason": "removed"},
        )
