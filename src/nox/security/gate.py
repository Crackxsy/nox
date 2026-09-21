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
gate that cannot be satisfied is a lock-out, not a protection. `nox onboard` and the Settings page
are where a PIN is set.
"""

from __future__ import annotations

from nox.core.state import PrivacyMode
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog
from nox.security.secrets import PinManager

log = get_logger(__name__)

__all__ = [
    "SECURITY_SETTING_PREFIXES",
    "PinRequiredError",
    "SecurityChangeGate",
    "relaxes_privacy",
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
    """A security-relevant change was attempted without the PIN it needs."""

    def __init__(self, action: str, reason: str) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason


def relaxes_privacy(current: PrivacyMode, target: PrivacyMode) -> bool:
    """Whether moving from `current` to `target` gives Nox more freedom than it has now."""
    return _MODE_ORDER.index(target) > _MODE_ORDER.index(current)


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
        """Whether a relaxing change needs a PIN right now."""
        return self._required and self._pin.is_set()

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        """Raise `PinRequiredError` unless `pin` unlocks `action`.

        Verification runs in a thread: the hash is deliberately expensive and this is called from
        IPC handlers on the event loop.
        """
        if not self.is_required():
            return
        if not pin:
            self._deny(action, by, "no PIN supplied")
            raise PinRequiredError(action, f"{action} requires the security PIN")
        status = await self._pin.verify_pin_async(pin, by=by)
        if status.ok:
            return
        self._deny(action, by, status.reason)
        raise PinRequiredError(action, f"{action} denied: {status.reason}")

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
