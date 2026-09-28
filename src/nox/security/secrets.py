"""The secret store and the PIN.

Secret names are `nox/<component>/<key>`; the keyring service is "nox" and the entry's user name
is the full secret name. Values are never logged, never shown in a repr and never audited.

The PIN is stored as an Argon2id hash (`argon2-cffi` is a dependency). PBKDF2-HMAC-SHA256 with 600k
iterations remains as the fallback for a broken install and as a format that is still verified: a
PBKDF2 hash written by an earlier version is re-hashed to Argon2id on its next successful
verification. Which scheme is in use is reported by `algorithm` and logged at startup: a silent
downgrade to the weaker scheme would be exactly the kind of invisible availability lie the project
forbids.

The keyring entry must hold a hash this module wrote. A raw value stored there by hand (with
`nox secrets set`, say) can never be verified; it is reported as `invalid_entry`, with the way out
(`nox pin set`), instead of looking like a wrong PIN forever.

Five failed attempts lock the PIN for fifteen minutes, and the counter is **persisted**. It used
to live in process memory only, so restarting the core - which the supervisor will do on request -
reset it and made the PIN brute-forceable one restart at a time. Verification itself is
deliberately slow, so `verify_pin_async` runs it in a thread; only a synchronous caller, a CLI or
a test, should use `verify_pin` directly. Verifications are serialised by a lock: without it,
concurrent requests all read the same failure count before any of them wrote it back, and a burst
of guesses counted as one attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import re
import secrets as _secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog, SecretStore
from nox.security.pin_attempts import InMemoryPinAttemptStore, PinAttemptState, PinAttemptStore

log = get_logger(__name__)

SECRET_NAME_RE = re.compile(r"^nox/[a-z0-9_\-]+/[a-z0-9_\-.]+$")
KEYRING_SERVICE = "nox"
PIN_SECRET_NAME = "nox/security/pin"  # noqa: S105 - keyring entry name, not a value
PBKDF2_ITERATIONS = 600_000
#: The shortest PIN accepted. A four-digit PIN falls to the lockout-limited guess rate in about
#: three weeks.
MIN_PIN_LENGTH = 6
#: The longest PIN accepted: a bound on the work one hash does, not a recommendation.
MAX_PIN_LENGTH = 64
#: What the user is told when the keyring entry is not a hash this module wrote.
INVALID_PIN_ENTRY_REASON = "PIN entry is not a Nox PIN hash - set it again with `nox pin set`"
_ARGON2_PREFIX = "argon2id$"
_PBKDF2_PREFIX = "pbkdf2_sha256$"
Clock = Callable[[], datetime]


class SecretNameError(ValueError):
    """Secret names must look like nox/<component>/<key> (lowercase)."""


class SecretStoreUnavailableError(RuntimeError):
    """The operating system's credential store cannot be reached.

    Raised, never turned into "no such secret": a PIN that cannot be read is not a PIN that was
    never set, and treating it as one would let a PIN-protected change through unasked.
    """


#: What to do when no credential store is running - Windows and macOS always have one.
KEYRING_UNAVAILABLE_HINT = (
    "no credential store is available: on Linux, run a Secret Service provider "
    "(GNOME Keyring or KWallet) in your desktop session"
)


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
        name = validate_name(name)
        with _backend_errors():
            value = self._keyring.get_password(self._service, name)
        return None if value is None else str(value)

    def set(self, name: str, value: str) -> None:
        name = validate_name(name)
        with _backend_errors():
            self._keyring.set_password(self._service, name, value)
        log.debug("secrets.set", name=name)

    def delete(self, name: str) -> None:
        validate_name(name)
        try:
            with _backend_errors():
                self._keyring.delete_password(self._service, name)
        except Exception as exc:  # noqa: BLE001 - keyring raises backend-specific errors for "not found"
            if type(exc).__name__ != "PasswordDeleteError":
                raise
        log.debug("secrets.deleted", name=name)

    def __repr__(self) -> str:
        return f"KeyringSecretStore(service={self._service!r})"


@contextmanager
def _backend_errors() -> Iterator[None]:
    """Turn keyring's "no backend" into `SecretStoreUnavailableError` with an actionable reason.

    Matched by name, because the error class lives in the optional `keyring` import.
    """
    try:
        yield
    except Exception as exc:
        if type(exc).__name__ in {"NoKeyringError", "InitError"}:
            raise SecretStoreUnavailableError(KEYRING_UNAVAILABLE_HINT) from exc
        raise


# ---- PIN -----------------------------------------------------------------------------------------


class PinCode(StrEnum):
    """Why a verification ended the way it did: stable words a UI can translate."""

    OK = "ok"
    WRONG = "pin_wrong"
    LOCKED = "locked"
    NOT_SET = "not_set"
    INVALID_ENTRY = "invalid_entry"
    BACKEND_MISSING = "backend_missing"


class PinEntryState(StrEnum):
    """What the credential store holds under `nox/security/pin`."""

    NOT_SET = "not_set"
    VALID = "valid"
    #: Not a hash this module wrote - typically a raw PIN typed into `nox secrets set`. No PIN can
    #: ever match it.
    INVALID = "invalid"


class PinPolicyError(ValueError):
    """A new PIN breaks the length rules. `code` is `too_short` or `too_long`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PinStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    locked: bool = False
    locked_until: datetime | None = None
    remaining_attempts: int = 0
    reason: str = ""
    code: PinCode = PinCode.OK


def looks_like_pin_hash(value: str) -> bool:
    """Whether `value` has the shape of a hash `PinManager` writes (not whether it is intact)."""
    if value.startswith(_ARGON2_PREFIX):
        return value[len(_ARGON2_PREFIX) :].startswith("$argon2id$")
    if value.startswith(_PBKDF2_PREFIX):
        return len(value[len(_PBKDF2_PREFIX) :].split("$")) == 3
    return False


def check_pin_policy(pin: str) -> None:
    """Raise `PinPolicyError` unless `pin` has an acceptable length."""
    if len(pin) < MIN_PIN_LENGTH:
        raise PinPolicyError("too_short", f"PIN must have at least {MIN_PIN_LENGTH} characters")
    if len(pin) > MAX_PIN_LENGTH:
        raise PinPolicyError("too_long", f"PIN must have at most {MAX_PIN_LENGTH} characters")


#: Why Argon2id is unavailable, for health and for the message the user sees. Empty means it is
#: in use.
ARGON2_MISSING_REASON = "argon2-cffi is not installed"


def _load_argon2() -> tuple[Any | None, str]:
    """`(hasher, reason)`. A missing or broken backend is reported, never silently swallowed."""
    try:
        module = importlib.import_module("argon2")
    except ImportError:
        return None, ARGON2_MISSING_REASON
    try:
        low_level = importlib.import_module("argon2.low_level")
        return module.PasswordHasher(type=low_level.Type.ID), ""
    except Exception as exc:  # noqa: BLE001 - any broken install downgrades, but visibly
        return None, f"argon2-cffi is installed but unusable: {type(exc).__name__}: {exc}"


class PinManager:
    def __init__(
        self,
        store: SecretStore,
        *,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
        max_attempts: int = 5,
        lockout_s: float = 15 * 60,
        prefer_argon2: bool = True,
        attempts: PinAttemptStore | None = None,
    ) -> None:
        self._store = store
        self._audit = SafeAuditLog(audit)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._lockout = timedelta(seconds=lockout_s)
        self._verify_lock = threading.Lock()
        self._argon2, self._argon2_reason = (
            _load_argon2() if prefer_argon2 else (None, "disabled by the caller")
        )
        if self._argon2 is None:
            log.warning("security.pin_hash_downgraded", reason=self._argon2_reason)
        self._attempts: PinAttemptStore = attempts or InMemoryPinAttemptStore()

    @property
    def algorithm(self) -> str:
        return "argon2id" if self._argon2 is not None else "pbkdf2_sha256"

    @property
    def hash_backend_reason(self) -> str:
        """Empty while Argon2id is in use, otherwise why the weaker scheme is active."""
        return self._argon2_reason

    def is_set(self) -> bool:
        return self._store.get(PIN_SECRET_NAME) is not None

    def entry_state(self) -> PinEntryState:
        """What the store holds. Raises `SecretStoreUnavailableError` when it cannot be read."""
        stored = self._store.get(PIN_SECRET_NAME)
        if stored is None:
            return PinEntryState.NOT_SET
        return PinEntryState.VALID if looks_like_pin_hash(stored) else PinEntryState.INVALID

    @property
    def locked_until(self) -> datetime | None:
        """When the current lockout ends, or None while there is none."""
        return self._attempts.load().locked_until if self.is_locked() else None

    def is_locked(self) -> bool:
        state = self._attempts.load()
        if state.locked_until is None:
            return False
        if self._clock() >= state.locked_until:
            self._attempts.clear()
            return False
        return True

    def set_pin(self, pin: str, *, by: str = "user") -> None:
        """Store `pin` as a hash, replacing whatever was there.

        Who may replace a PIN is decided by the caller: `nox.security.pin_setup` asks for the
        current one first.
        """
        check_pin_policy(pin)
        self._store.set(PIN_SECRET_NAME, self._hash(pin))
        self._attempts.clear()
        self._audit.append(
            actor=by, action="pin.set", target="pin", details={"algorithm": self.algorithm}
        )
        log.info("security.pin_set", algorithm=self.algorithm)

    def clear_pin(self, *, by: str = "user") -> None:
        self._store.delete(PIN_SECRET_NAME)
        self._audit.append(actor=by, action="pin.clear", target="pin")

    async def verify_pin_async(self, pin: str, *, by: str = "user") -> PinStatus:
        """`verify_pin` in a worker thread.

        Argon2id is intentionally slow - hundreds of milliseconds - and this is called from IPC
        handlers running on the event loop.
        """
        return await asyncio.to_thread(self.verify_pin, pin, by=by)

    def verify_pin(self, pin: str, *, by: str = "user") -> PinStatus:
        with self._verify_lock:
            return self._verify_serialised(pin, by=by)

    def _verify_serialised(self, pin: str, *, by: str) -> PinStatus:
        state = self._attempts.load()
        if self.is_locked():
            return PinStatus(
                ok=False,
                locked=True,
                locked_until=state.locked_until,
                remaining_attempts=0,
                reason="locked out",
                code=PinCode.LOCKED,
            )
        stored = self._store.get(PIN_SECRET_NAME)
        if stored is None:
            return PinStatus(
                ok=False,
                remaining_attempts=self._remaining(state),
                reason="no PIN set",
                code=PinCode.NOT_SET,
            )
        if not looks_like_pin_hash(stored):
            return self._invalid_entry(state, by=by)
        if stored.startswith(_ARGON2_PREFIX) and self._argon2 is None:
            # Not "wrong PIN": the stored hash is fine and the entered PIN may be too. Saying so
            # is the difference between a user retyping forever and a user reinstalling a package.
            log.error("security.pin_backend_missing", reason=self._argon2_reason)
            return PinStatus(
                ok=False,
                remaining_attempts=self._remaining(state),
                reason=f"PIN hashing backend missing ({self._argon2_reason})",
                code=PinCode.BACKEND_MISSING,
            )
        if self._verify(stored, pin):
            self._attempts.clear()
            self._audit.append(actor=by, action="pin.verify", target="pin")
            if not stored.startswith(_ARGON2_PREFIX):
                self._upgrade_hash(pin, by=by)
            return PinStatus(ok=True, remaining_attempts=self._max_attempts)
        return self._count_failure(state, by=by)

    def _invalid_entry(self, state: PinAttemptState, *, by: str) -> PinStatus:
        """The entry is not a hash this module wrote, so no PIN can ever match it.

        Not counted as a failed attempt: the user has to be told how to repair the entry, not be
        locked out for retyping a PIN that was never the problem.
        """
        log.error("security.pin_entry_invalid")
        self._audit.append(
            actor=by,
            action="pin.verify",
            target="pin",
            decision="deny",
            result="denied",
            details={"reason": PinCode.INVALID_ENTRY.value},
        )
        return PinStatus(
            ok=False,
            remaining_attempts=self._remaining(state),
            reason=INVALID_PIN_ENTRY_REASON,
            code=PinCode.INVALID_ENTRY,
        )

    def _count_failure(self, state: PinAttemptState, *, by: str) -> PinStatus:
        failed = state.failed + 1
        details = {"failed_attempts": str(failed)}
        if failed >= self._max_attempts:
            locked_until = self._clock() + self._lockout
            self._attempts.save(PinAttemptState(failed=failed, locked_until=locked_until))
            details["locked_until"] = locked_until.isoformat()
            self._audit.append(
                actor=by,
                action="pin.lockout",
                target="pin",
                decision="deny",
                result="denied",
                details=details,
            )
            log.warning("security.pin_lockout", failed_attempts=failed)
            return PinStatus(
                ok=False,
                locked=True,
                locked_until=locked_until,
                remaining_attempts=0,
                reason="too many failed attempts",
                code=PinCode.LOCKED,
            )
        self._attempts.save(PinAttemptState(failed=failed, locked_until=None))
        self._audit.append(
            actor=by,
            action="pin.verify",
            target="pin",
            decision="deny",
            result="denied",
            details=details,
        )
        log.warning("security.pin_verify_failed", failed_attempts=failed)
        return PinStatus(
            ok=False,
            remaining_attempts=max(self._max_attempts - failed, 0),
            reason="wrong PIN",
            code=PinCode.WRONG,
        )

    def _upgrade_hash(self, pin: str, *, by: str) -> None:
        """Replace a verified PBKDF2 hash with an Argon2id one; the PIN itself stays the same.

        Only possible right after a successful verification, the one moment the PIN is in hand.
        A failed write keeps the old hash, which still verifies, and the next success tries again.
        """
        if self._argon2 is None:
            return
        try:
            self._store.set(PIN_SECRET_NAME, self._hash(pin))
        except Exception as exc:  # noqa: BLE001 - the verification itself succeeded either way
            log.warning("security.pin_rehash_failed", error=type(exc).__name__)
            return
        self._audit.append(
            actor=by, action="pin.rehash", target="pin", details={"algorithm": "argon2id"}
        )
        log.info("security.pin_rehashed", algorithm="argon2id")

    # ---- hashing ---------------------------------------------------------------------------------

    def _remaining(self, state: PinAttemptState) -> int:
        return max(self._max_attempts - state.failed, 0)

    def _hash(self, pin: str) -> str:
        if self._argon2 is not None:
            return _ARGON2_PREFIX + str(self._argon2.hash(pin))
        salt = _secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

    def _verify(self, stored: str, pin: str) -> bool:
        scheme, _, rest = stored.partition("$")
        if scheme == "argon2id":
            if self._argon2 is None:  # handled by the caller, which reports the real reason
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
