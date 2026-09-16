"""Secret store and PIN (Security Model §7/§9, model.py SecretStore, engineering brief "secrets only
via keyring").

Names are `nox/<component>/<key>`; the keyring service is "nox" and the username is the full name.
Values are never logged, never included in reprs, never audited. The PIN is stored as an Argon2id
hash (`argon2-cffi`, optional) or PBKDF2-HMAC-SHA256 with 600k iterations (hashlib fallback).
5 failed attempts -> 15 min lockout, audited.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import re
import secrets as _secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from nox.security._logging import get_logger
from nox.security.model import AuditLog, SecretStore

log = get_logger(__name__)

SECRET_NAME_RE = re.compile(r"^nox/[a-z0-9_\-]+/[a-z0-9_\-.]+$")
KEYRING_SERVICE = "nox"
PIN_SECRET_NAME = "nox/security/pin"  # noqa: S105 - keyring entry name, not a value
PBKDF2_ITERATIONS = 600_000
Clock = Callable[[], datetime]


class SecretNameError(ValueError):
    """Secret names must look like nox/<component>/<key> (lowercase)."""


def validate_name(name: str) -> str:
    if not SECRET_NAME_RE.match(name):
        raise SecretNameError(f"invalid secret name {name!r}; expected nox/<component>/<key>")
    return name


class InMemorySecretStore:
    """Test/dev backend. Holds values only in process memory."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(validate_name(name))

    def set(self, name: str, value: str) -> None:
        self._values[validate_name(name)] = value

    def delete(self, name: str) -> None:
        self._values.pop(validate_name(name), None)

    def names(self) -> list[str]:
        return sorted(self._values)

    def __repr__(self) -> str:
        return f"InMemorySecretStore(count={len(self._values)})"


class KeyringSecretStore:
    """Windows Credential Manager (or the platform keyring) via `keyring`."""

    def __init__(self, service: str = KEYRING_SERVICE, backend: Any | None = None) -> None:
        self._service = service
        self._keyring = backend if backend is not None else importlib.import_module("keyring")

    def get(self, name: str) -> str | None:
        value = self._keyring.get_password(self._service, validate_name(name))
        return None if value is None else str(value)

    def set(self, name: str, value: str) -> None:
        self._keyring.set_password(self._service, validate_name(name), value)
        log.debug("secrets.set", name=name)

    def delete(self, name: str) -> None:
        validate_name(name)
        try:
            self._keyring.delete_password(self._service, name)
        except Exception as exc:  # noqa: BLE001 - keyring raises backend-specific errors for "not found"
            if type(exc).__name__ != "PasswordDeleteError":
                raise
        log.debug("secrets.deleted", name=name)

    def __repr__(self) -> str:
        return f"KeyringSecretStore(service={self._service!r})"


# ---- PIN -----------------------------------------------------------------------------------------


class PinStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    locked: bool = False
    locked_until: datetime | None = None
    remaining_attempts: int = 0
    reason: str = ""


def _load_argon2() -> Any | None:
    try:
        module = importlib.import_module("argon2")
    except ImportError:
        return None
    try:
        low_level = importlib.import_module("argon2.low_level")
        return module.PasswordHasher(type=low_level.Type.ID)
    except Exception:  # noqa: BLE001
        return None


class PinManager:
    def __init__(
        self,
        store: SecretStore,
        *,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
        max_attempts: int = 5,
        lockout_s: float = 15 * 60,
        min_length: int = 4,
        prefer_argon2: bool = True,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._lockout = timedelta(seconds=lockout_s)
        self._min_length = min_length
        self._argon2 = _load_argon2() if prefer_argon2 else None
        self._failed = 0
        self._locked_until: datetime | None = None

    @property
    def algorithm(self) -> str:
        return "argon2id" if self._argon2 is not None else "pbkdf2_sha256"

    def is_set(self) -> bool:
        return self._store.get(PIN_SECRET_NAME) is not None

    def is_locked(self) -> bool:
        if self._locked_until is None:
            return False
        if self._clock() >= self._locked_until:
            self._locked_until = None
            self._failed = 0
            return False
        return True

    def set_pin(self, pin: str, *, by: str = "user") -> None:
        if len(pin) < self._min_length:
            raise ValueError(f"PIN must have at least {self._min_length} characters")
        self._store.set(PIN_SECRET_NAME, self._hash(pin))
        self._failed = 0
        self._locked_until = None
        self._audit_append(
            actor=by,
            action="pin.set",
            decision="allow",
            result="ok",
            details={"algorithm": self.algorithm},
        )
        log.info("security.pin_set", algorithm=self.algorithm)

    def clear_pin(self, *, by: str = "user") -> None:
        self._store.delete(PIN_SECRET_NAME)
        self._audit_append(actor=by, action="pin.clear", decision="allow", result="ok")

    def verify_pin(self, pin: str, *, by: str = "user") -> PinStatus:
        if self.is_locked():
            return PinStatus(
                ok=False,
                locked=True,
                locked_until=self._locked_until,
                remaining_attempts=0,
                reason="locked out",
            )
        stored = self._store.get(PIN_SECRET_NAME)
        if stored is None:
            return PinStatus(ok=False, remaining_attempts=self._remaining(), reason="no PIN set")
        if self._verify(stored, pin):
            self._failed = 0
            self._audit_append(actor=by, action="pin.verify", decision="allow", result="ok")
            return PinStatus(ok=True, remaining_attempts=self._max_attempts)
        self._failed += 1
        details = {"failed_attempts": str(self._failed)}
        if self._failed >= self._max_attempts:
            self._locked_until = self._clock() + self._lockout
            details["locked_until"] = self._locked_until.isoformat()
            self._audit_append(
                actor=by, action="pin.lockout", decision="deny", result="denied", details=details
            )
            log.warning("security.pin_lockout", failed_attempts=self._failed)
            return PinStatus(
                ok=False,
                locked=True,
                locked_until=self._locked_until,
                remaining_attempts=0,
                reason="too many failed attempts",
            )
        self._audit_append(
            actor=by, action="pin.verify", decision="deny", result="denied", details=details
        )
        log.warning("security.pin_verify_failed", failed_attempts=self._failed)
        return PinStatus(ok=False, remaining_attempts=self._remaining(), reason="wrong PIN")

    # ---- hashing ---------------------------------------------------------------------------------

    def _remaining(self) -> int:
        return max(self._max_attempts - self._failed, 0)

    def _hash(self, pin: str) -> str:
        if self._argon2 is not None:
            return "argon2id$" + str(self._argon2.hash(pin))
        salt = _secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

    def _verify(self, stored: str, pin: str) -> bool:
        scheme, _, rest = stored.partition("$")
        if scheme == "argon2id":
            if self._argon2 is None:
                log.error("security.pin_argon2_unavailable")
                return False
            try:
                return bool(self._argon2.verify(rest, pin))
            except Exception:  # noqa: BLE001 - VerifyMismatchError and friends
                return False
        if scheme == "pbkdf2_sha256":
            try:
                iterations_s, salt_hex, digest_hex = rest.split("$")
                digest = hashlib.pbkdf2_hmac(
                    "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations_s)
                )
                return hmac.compare_digest(digest.hex(), digest_hex)
            except ValueError:
                return False
        return False

    def _audit_append(
        self,
        *,
        actor: str,
        action: str,
        decision: str,
        result: str,
        details: dict[str, str] | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.append(
                actor=actor,
                tool="security",
                action=action,
                target="pin",
                decision=decision,
                result=result,
                details=details,
            )
