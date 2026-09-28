"""Setting, changing and removing the security PIN - the one set of rules every surface shares.

The dashboard (`security.pin.set` / `security.pin.clear`), `nox pin` and `nox onboard` all go
through `PinSetup`, so the rules cannot drift between them:

* the first PIN needs nothing - there is no secret yet to prove,
* changing or removing a PIN needs the current one, verified with the same lockout as every
  other PIN check,
* a new PIN has to meet the length rules (`MIN_PIN_LENGTH`..`MAX_PIN_LENGTH`),
* a credential-store entry that is not a PIN hash (a raw value typed into `nox secrets set`) is
  never accepted as a PIN. The dashboard refuses to touch it and names the fix; only the local
  command line, whose user can edit the credential store directly anyway, may replace or remove it
  (`allow_invalid_entry=True`).

Every refusal is a `PinChangeError` with a stable `code` a UI can translate and the facts it needs
(`remaining_attempts`, `locked_until`, `min_length`). Setting and clearing are audited by
`PinManager` itself; the PIN, its hash and its length never leave this module.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from nox.security.secrets import (
    INVALID_PIN_ENTRY_REASON,
    MAX_PIN_LENGTH,
    MIN_PIN_LENGTH,
    PinCode,
    PinEntryState,
    PinManager,
    PinPolicyError,
    PinStatus,
    SecretStoreUnavailableError,
    check_pin_policy,
)

__all__ = [
    "PIN_CODES_PERMISSION",
    "PIN_CODES_UNAVAILABLE",
    "PinChangeError",
    "PinSetup",
    "pin_error_from_status",
]

#: Refusal codes that mean "you may not do this" (as opposed to "your input is malformed" or
#: "the credential store is not there").
PIN_CODES_PERMISSION: frozenset[str] = frozenset(
    {
        "pin_required",
        PinCode.WRONG.value,
        PinCode.LOCKED.value,
        PinCode.INVALID_ENTRY.value,
    }
)
#: Refusal codes that mean a part of the system is missing, not that the user did something wrong.
PIN_CODES_UNAVAILABLE: frozenset[str] = frozenset(
    {"store_unavailable", PinCode.BACKEND_MISSING.value}
)


class PinChangeError(Exception):
    """A PIN change was refused. `code` is stable; `message` is a sentence for a log or a CLI."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def payload(self) -> dict[str, Any]:
        """The facts a UI needs, JSON-ready: always `reason`, plus whatever applies."""
        return {"reason": self.code, **self.details}


def pin_error_from_status(status: PinStatus, *, action: str) -> PinChangeError:
    """Turn a failed verification into the refusal a caller raises."""
    details: dict[str, Any] = {}
    if status.code is PinCode.WRONG:
        details["remaining_attempts"] = status.remaining_attempts
    if status.locked_until is not None:
        details["locked_until"] = _iso(status.locked_until)
    return PinChangeError(status.code.value, f"{action} denied: {status.reason}", details)


class PinSetup:
    """The rules above, on one `PinManager`."""

    def __init__(self, pin: PinManager) -> None:
        self._pin = pin

    def state(self) -> PinEntryState:
        """What the credential store holds; raises `PinChangeError(store_unavailable)`."""
        try:
            return self._pin.entry_state()
        except SecretStoreUnavailableError as exc:
            raise PinChangeError("store_unavailable", str(exc)) from exc

    async def set(
        self,
        new_pin: str,
        *,
        current_pin: str | None,
        by: str,
        allow_invalid_entry: bool = False,
    ) -> None:
        """Set the first PIN, or replace the current one (which then has to be `current_pin`)."""
        try:
            check_pin_policy(new_pin)
        except PinPolicyError as exc:
            raise PinChangeError(
                exc.code,
                str(exc),
                {"min_length": MIN_PIN_LENGTH, "max_length": MAX_PIN_LENGTH},
            ) from exc
        await self._authorise(current_pin, by=by, action="pin.set", allow=allow_invalid_entry)
        await asyncio.to_thread(self._pin.set_pin, new_pin, by=by)

    async def clear(
        self, *, current_pin: str | None, by: str, allow_invalid_entry: bool = False
    ) -> None:
        """Remove the PIN; only with the current one. Without a PIN there is nothing to remove."""
        if self.state() is PinEntryState.NOT_SET:
            raise PinChangeError(PinCode.NOT_SET.value, "no PIN is set")
        await self._authorise(current_pin, by=by, action="pin.clear", allow=allow_invalid_entry)
        await asyncio.to_thread(self._pin.clear_pin, by=by)

    async def _authorise(
        self, current_pin: str | None, *, by: str, action: str, allow: bool
    ) -> None:
        state = self.state()
        if state is PinEntryState.NOT_SET:
            return
        if state is PinEntryState.INVALID:
            if allow:
                return
            raise PinChangeError(PinCode.INVALID_ENTRY.value, INVALID_PIN_ENTRY_REASON)
        if not current_pin:
            raise PinChangeError("pin_required", f"{action} requires the current PIN")
        status = await self._pin.verify_pin_async(current_pin, by=by)
        if not status.ok:
            raise pin_error_from_status(status, action=action)


def _iso(value: datetime) -> str:
    return value.isoformat()
