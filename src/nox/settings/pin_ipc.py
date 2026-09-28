"""`security.pin.status` / `security.pin.set` / `security.pin.clear`: the PIN from the dashboard.

The rules themselves - first PIN free, changing and removing only with the current one, length
limits, a raw credential-store entry never accepted - live in `nox.security.pin_setup`, shared with
`nox pin` and `nox onboard`. This module adds what only the IPC surface needs:

* a rate limit of its own, tighter than the one on credentials (a PIN is changed a few times a
  year; a burst of requests is somebody guessing),
* the refusal as an `IpcError` whose `details.reason` a UI can translate (`pin_required`,
  `pin_wrong`, `locked`, `too_short`, `invalid_entry`, ...), with `remaining_attempts` or
  `locked_until` where they apply,
* an audit entry for every refused change - a successful one is audited by `PinManager`.

`security.pin.status` answers what the Settings and Status pages need to decide whether to show a
PIN field, and nothing about the PIN itself: never its length, hash or algorithm.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nox.core.logging import get_logger
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, ERR_VALIDATION, IpcError
from nox.security.model import AuditLog
from nox.security.pin_setup import (
    PIN_CODES_PERMISSION,
    PIN_CODES_UNAVAILABLE,
    PinChangeError,
    PinSetup,
)
from nox.security.secrets import MIN_PIN_LENGTH, PinEntryState, PinManager
from nox.settings.secrets_ipc import RateLimiter

log = get_logger(__name__)

__all__ = ["PIN_RATE_LIMIT", "PIN_RATE_WINDOW_S", "PinService"]

#: PIN changes per window. A person sets a PIN, maybe mistypes the old one twice; more than this in
#: a minute is not a person.
PIN_RATE_LIMIT = 5
PIN_RATE_WINDOW_S = 60.0


class PinService:
    """What the `security.pin.*` handlers call."""

    def __init__(
        self,
        pin: PinManager,
        *,
        audit: AuditLog | None = None,
        gate_required: Callable[[], bool] = lambda: False,
        resume_requires_pin: Callable[[], bool] = lambda: False,
        max_calls: int = PIN_RATE_LIMIT,
        window_s: float = PIN_RATE_WINDOW_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._pin = pin
        self._setup = PinSetup(pin)
        self._audit = audit
        self._gate_required = gate_required
        self._resume_requires_pin = resume_requires_pin
        self._limiter = RateLimiter(max_calls, window_s, clock, what="PIN changes")

    def status(self) -> dict[str, Any]:
        """`{state, configured, min_length, gate_required, resume_requires_pin, locked_until}`.

        `configured` keeps its old meaning - "a PIN-gated request has to carry a PIN" - so an entry
        that cannot be read or cannot be verified counts as configured: failing closed.
        """
        try:
            state = self._pin.entry_state().value
        except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, not raised
            log.warning("security.pin_status_unavailable", error=type(exc).__name__)
            state = "unavailable"
        locked_until = self._pin.locked_until
        return {
            "state": state,
            "configured": state != PinEntryState.NOT_SET.value,
            "min_length": MIN_PIN_LENGTH,
            "gate_required": self._gate_required(),
            "resume_requires_pin": self._resume_requires_pin(),
            "locked_until": locked_until.isoformat() if locked_until is not None else None,
        }

    async def set(self, new_pin: str, *, current_pin: str | None, by: str) -> dict[str, Any]:
        self._limiter.check()
        try:
            await self._setup.set(new_pin, current_pin=current_pin, by=by)
        except PinChangeError as exc:
            raise self._refuse(exc, action="pin.set", by=by) from exc
        log.info("security.pin_set_via_ipc", by=by)
        return {"ok": True, "state": PinEntryState.VALID.value}

    async def clear(self, *, current_pin: str | None, by: str) -> dict[str, Any]:
        self._limiter.check()
        try:
            await self._setup.clear(current_pin=current_pin, by=by)
        except PinChangeError as exc:
            raise self._refuse(exc, action="pin.clear", by=by) from exc
        log.info("security.pin_cleared_via_ipc", by=by)
        return {"ok": True, "state": PinEntryState.NOT_SET.value}

    def _refuse(self, exc: PinChangeError, *, action: str, by: str) -> IpcError:
        if self._audit is not None:
            self._audit.append(
                actor=by,
                tool="settings",
                action=action,
                target="pin",
                decision="deny",
                result="denied",
                details={"reason": exc.code},
            )
        log.warning("security.pin_change_refused", action=action, by=by, reason=exc.code)
        if exc.code in PIN_CODES_PERMISSION:
            code = ERR_PERMISSION
        elif exc.code in PIN_CODES_UNAVAILABLE:
            code = ERR_UNAVAILABLE
        else:
            code = ERR_VALIDATION
        return IpcError(code, exc.message, details=exc.payload())
