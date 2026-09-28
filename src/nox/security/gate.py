"""The PIN gate in front of changes that relax the security posture.

`security.pin_required_for_security_changes` is a promise the product makes in its own
configuration file. This module is what keeps it: when the setting is on **and** a PIN is
configured, a change that weakens protection has to carry that PIN.

What counts as relaxing, and what deliberately does not:

* leaving a stricter privacy mode (private or offline -> balanced or full) is gated; switching
  *to* a stricter mode never is, because a user reaching for more privacy must not be stopped,
  and a forgotten PIN must never be able to trap Nox in an open state,
* turning a capture device back on is gated; turning one off is not,
* editing a `security.*` or `privacy.*` setting from the dashboard is gated,
* changing a stored credential is gated - that check already lived in the secrets service and
  stays there,
* switching the assistant mode is not gated. The profile it selects comes from a vetted file and
  every mode is meant to be one click away; asking for a PIN to switch to coding mode would only
  teach the user to type it without reading.

Without a PIN configured - a fresh install - nothing is gated: there is no secret to prove, and a
gate that cannot be satisfied is a lock-out, not a protection. `nox onboard`, `nox pin set` and the
dashboard's Settings page are where a PIN is set (all three through `nox.security.pin_setup`).

When the operating system's credential store cannot be read (a Linux session without a Secret
Service), whether a PIN exists is unknown. Relaxing changes are then refused with that reason -
"cannot tell" is never treated as "no PIN".
"""

from __future__ import annotations

from typing import Any

from nox.core.state import PrivacyMode
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog
from nox.security.pin_setup import pin_error_from_status
from nox.security.secrets import PinManager, SecretStoreUnavailableError

log = get_logger(__name__)

__all__ = [
    "SECURITY_SETTING_PREFIXES",
    "PinRequiredError",
    "SecurityChangeGate",
    "relaxes_privacy",
    "strictest_privacy_mode",
]

#: Configuration paths whose change is a security change.
SECURITY_SETTING_PREFIXES: tuple[str, ...] = ("security.", "privacy.")

#: Privacy modes ordered from most to least protective. A move toward the end relaxes.
_MODE_ORDER: tuple[PrivacyMode, ...] = (
    PrivacyMode.OFFLINE,
    PrivacyMode.PRIVATE,
    PrivacyMode.BALANCED,
    PrivacyMode.FULL,
)


class PinRequiredError(PermissionError):
    """A security-relevant change was attempted without the PIN it needs.

    `details` carries the stable `reason` code (`pin_required`, `pin_wrong`, `locked`,
    `invalid_entry`, `store_unavailable`) and the facts that go with it, so a UI can say exactly
    what happened instead of pattern-matching the English sentence.
    """

    def __init__(self, action: str, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {"reason": "pin_required"})


def relaxes_privacy(current: PrivacyMode, target: PrivacyMode) -> bool:
    """Whether moving from `current` to `target` gives Nox more freedom than it has now."""
    return _MODE_ORDER.index(target) > _MODE_ORDER.index(current)


def strictest_privacy_mode(*modes: PrivacyMode) -> PrivacyMode:
    """The most protective of `modes`; OFFLINE when none is given (nothing known is strictest)."""
    if not modes:
        return PrivacyMode.OFFLINE
    return min(modes, key=_MODE_ORDER.index)


class SecurityChangeGate:
    """Verifies the PIN for a relaxing change, or explains why one is needed."""

    def __init__(
        self,
        pin: PinManager,
        *,
        required: bool,
        audit: AuditLog | None = None,
    ) -> None:
        self._pin = pin
        self._required = required
        self._audit = SafeAuditLog(audit)

    @property
    def configured(self) -> bool:
        """Whether the setting is on at all, independently of whether a PIN exists."""
        return self._required

    def is_required(self) -> bool:
        """Whether a relaxing change needs a PIN right now. Unknown counts as yes."""
        if not self._required:
            return False
        try:
            return self._pin.is_set()
        except SecretStoreUnavailableError:
            return True

    def describe(self) -> str:
        """`on`, `off` or `unknown` (credential store unreadable), for logs and status."""
        if not self._required:
            return "off"
        try:
            return "on" if self._pin.is_set() else "off"
        except SecretStoreUnavailableError:
            return "unknown"

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        """Raise `PinRequiredError` unless `pin` unlocks `action`.

        Verification runs in a thread: the hash is deliberately expensive and this is called from
        IPC handlers on the event loop.
        """
        if not self.is_required():
            return
        if self.describe() == "unknown":
            self._deny(action, by, "credential store unavailable")
            raise PinRequiredError(
                action,
                f"{action} denied: the credential store holding the PIN cannot be read",
                {"reason": "store_unavailable"},
            )
        if not pin:
            self._deny(action, by, "no PIN supplied")
            raise PinRequiredError(action, f"{action} requires the security PIN")
        status = await self._pin.verify_pin_async(pin, by=by)
        if status.ok:
            return
        self._deny(action, by, status.reason)
        refusal = pin_error_from_status(status, action=action)
        raise PinRequiredError(action, refusal.message, refusal.payload())

    def _deny(self, action: str, by: str, reason: str) -> None:
        log.warning("security.pin_gate_denied", action=action, by=by, reason=reason)
        self._audit.append(
            actor=by,
            action=action,
            target="security_change",
            decision="deny",
            result="denied",
            details={"reason": reason},
        )
