"""`secrets.status` / `secrets.set` / `secrets.delete`: manage credentials without ever showing one.

The Security Model's rule is that secrets live in the Windows Credential Manager and nowhere else -
not in a config file, not in a log line, not in a prompt, and not in the audit log. This service is
the dashboard's way to satisfy that rule instead of working around it, so it is deliberately
narrow:

* only the *known* names below may be written or deleted (a caller cannot create arbitrary keyring
  entries through the UI, and cannot touch `nox/security/pin`),
* a value is never returned, never logged and never audited - `secrets.status` answers with a
  boolean `present` per name,
* writes and deletes are rate-limited (a UI action a user takes a handful of times a year should
  never be usable as a brute-force or keyring-thrash channel),
* when a PIN is configured, changing a secret requires it, the same way resuming from a
  security-path kill does.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from nox.core.logging import get_logger
from nox.ipc.errors import ERR_PERMISSION, ERR_RATE_LIMITED, ERR_VALIDATION, IpcError
from nox.security.model import AuditLog, SecretStore
from nox.security.secrets import PinManager

log = get_logger(__name__)

#: The secret names the Settings page manages, and the integration each belongs to. Anything not
#: listed here is refused - including `nox/security/pin`, which has its own PIN-gated path.
KNOWN_SECRETS: dict[str, str] = {
    "nox/twitch/oauth_token": "twitch",
    "nox/twitch/refresh_token": "twitch",
    "nox/twitch/bot_username": "twitch",
    "nox/twitch/client_id": "twitch",
    "nox/obs/websocket_password": "obs",
    "nox/telegram/bot_token": "telegram",
}

DEFAULT_RATE_LIMIT = 10
DEFAULT_RATE_WINDOW_S = 60.0


class _RateLimiter:
    """A sliding window over write attempts; shared by `secrets.set` and `secrets.delete`."""

    def __init__(
        self,
        max_calls: int = DEFAULT_RATE_LIMIT,
        window_s: float = DEFAULT_RATE_WINDOW_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max = max_calls
        self._window = window_s
        self._clock = clock or time.monotonic
        self._calls: deque[float] = deque()

    def check(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= self._window:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            raise IpcError(
                ERR_RATE_LIMITED,
                f"at most {self._max} secret changes per {self._window:.0f}s",
                retryable=True,
            )
        self._calls.append(now)


class SecretsService:
    """What the `secrets.*` IPC handlers call. No `get` exists on purpose."""

    def __init__(
        self,
        store: SecretStore,
        *,
        pin: PinManager | None = None,
        audit: AuditLog | None = None,
        max_calls: int = DEFAULT_RATE_LIMIT,
        window_s: float = DEFAULT_RATE_WINDOW_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._pin = pin
        self._audit = audit
        self._limiter = _RateLimiter(max_calls, window_s, clock)

    # -- read ------------------------------------------------------------------------------------

    def status(self) -> dict[str, object]:
        """Presence only, never a value - not even a masked or truncated one."""
        secrets = []
        for name, group in KNOWN_SECRETS.items():
            try:
                present = self._store.get(name) is not None
            except Exception as exc:  # noqa: BLE001 - an unavailable keyring is "not present"
                log.warning("secrets.status_failed", name=name, error=type(exc).__name__)
                present = False
            secrets.append({"name": name, "present": present, "group": group})
        return {"secrets": secrets}

    # -- write -----------------------------------------------------------------------------------

    async def set(self, name: str, value: str, *, pin: str | None, by: str) -> dict[str, object]:
        await self._guard(name, pin=pin, by=by, action="secret.set")
        if not value:
            raise IpcError(ERR_VALIDATION, "value must not be empty")
        self._store.set(name, value)
        self._audit_name(name, action="secret.set", by=by)
        log.info("secrets.set", name=name, by=by)  # name only - never the value
        return {"ok": True}

    async def delete(self, name: str, *, pin: str | None, by: str) -> dict[str, object]:
        await self._guard(name, pin=pin, by=by, action="secret.delete")
        self._store.delete(name)
        self._audit_name(name, action="secret.delete", by=by)
        log.info("secrets.deleted", name=name, by=by)
        return {"ok": True}

    # -- helpers ---------------------------------------------------------------------------------

    async def _guard(self, name: str, *, pin: str | None, by: str, action: str) -> None:
        """A known name, inside the rate limit, and - when a PIN is set - verified.

        Verification is deliberately expensive, so it runs in a thread rather than on the event
        loop this handler was called from.
        """
        if name not in KNOWN_SECRETS:
            raise IpcError(ERR_VALIDATION, f"unknown secret name {name!r}")
        self._limiter.check()
        if self._pin is None or not self._pin.is_set():
            return
        verified = pin is not None and (await self._pin.verify_pin_async(pin, by=by)).ok
        if not verified:
            self._audit_name(name, action=action, by=by, decision="deny", result="denied")
            raise IpcError(ERR_PERMISSION, "PIN required to change a stored secret")

    def _audit_name(
        self,
        name: str,
        *,
        action: str,
        by: str,
        decision: str = "allow",
        result: str = "ok",
    ) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor=by,
            tool="settings",
            action=action,
            target=name,  # the secret *name*; the value never enters the audit log
            decision=decision,
            result=result,
        )
