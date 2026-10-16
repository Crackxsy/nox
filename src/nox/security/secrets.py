"""The secret store and the PIN.

Secret names are `nox/<component>/<key>`; the keyring service is "nox" and the entry's user name
is the full secret name. Values are never logged, never shown in a repr and never audited.

The PIN is stored as an Argon2id hash when `argon2-cffi` is installed, and as PBKDF2-HMAC-SHA256
with 600k iterations otherwise. Which one is in use is reported by `algorithm` and logged at
startup: a silent downgrade to the weaker scheme would be exactly the kind of invisible
availability lie the project forbids.

Five failed attempts lock the PIN for fifteen minutes, and the counter is **persisted**. It used
to live in process memory only, so restarting the core - which the supervisor will do on request -
reset it and made the PIN brute-forceable one restart at a time. Verification itself is
deliberately slow, so `verify_pin_async` runs it in a thread; only a synchronous caller, a CLI or
a test, should use `verify_pin` directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import secrets as _secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.model import AuditLog, SecretStore
from nox.security.pin_attempts import InMemoryPinAttemptStore, PinAttemptState, PinAttemptStore
from nox.security.secret_names import SECRET_NAME_RE as SECRET_NAME_RE  # re-exported
from nox.security.secret_names import SecretNameError as SecretNameError  # re-exported
from nox.security.secret_names import (
    SecretStoreError,
    SecretStoreUnavailableError,
    validate_name,
)

log = get_logger(__name__)

KEYRING_SERVICE = "nox"
PIN_SECRET_NAME = "nox/security/pin"  # noqa: S105 - keyring entry name, not a value
PBKDF2_ITERATIONS = 600_000
Clock = Callable[[], datetime]


class InMemorySecretStore:
    """Test/dev backend. Holds values only in process memory."""

    backend = "memory"

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

    backend = "keyring"

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


# ---- choosing a store ----------------------------------------------------------------------------

SecretBackend = Literal["auto", "keyring", "file"]

#: Which store this installation chose the first time. Read on every later start, so a keyring
#: that is missing one day (a service started before login, a broken D-Bus) is reported instead of
#: being silently replaced by an empty file - which would make the PIN look unset.
BACKEND_MARKER = "secret_backend"


class UnavailableSecretStore:
    """Stands in for a store that this installation uses but cannot reach right now.

    Every call raises `SecretStoreUnavailableError`. `PinManager` treats that as "a PIN is set
    and cannot be verified", so nothing that needs the PIN goes through.
    """

    backend = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def get(self, name: str) -> str | None:
        raise SecretStoreUnavailableError(self.reason)

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailableError(self.reason)

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailableError(self.reason)

    def __repr__(self) -> str:
        return f"UnavailableSecretStore(reason={self.reason!r})"


def keyring_usable(module: Any | None = None) -> bool:
    """Whether the platform keyring can actually hold a secret.

    `keyring` always returns *a* backend. Without a desktop session on Linux it is the `fail`
    backend (priority 0), or a chainer with nothing in it; both refuse every call.
    """
    try:
        kr = module if module is not None else importlib.import_module("keyring")
        backend = kr.get_keyring()
    except Exception:  # noqa: BLE001 - an unimportable or broken keyring is simply not usable
        return False
    if getattr(backend, "priority", 1) <= 0:
        return False
    chained = getattr(backend, "backends", None)
    return not (chained is not None and len(chained) == 0)


def _read_marker(home: Path) -> str | None:
    try:
        value = (home / BACKEND_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value in ("keyring", "file") else None


def _write_marker(home: Path, backend: str) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / BACKEND_MARKER).write_text(backend + "\n", encoding="utf-8")
    except OSError as exc:  # a read-only home is reported by the store itself on first use
        log.warning("secrets.marker_write_failed", error=type(exc).__name__)


def resolve_secret_backend(backend: SecretBackend = "auto", *, home: Path | None = None) -> str:
    """`keyring`, `file` or `unavailable` - the store this process must use, without opening it.

    `keyring`/`file` in the configuration force one. `auto` keeps whatever this installation
    chose before; only on first use does it pick the keyring where one works and the file where
    none does (a headless Raspberry Pi).
    """
    from nox.paths import nox_home  # noqa: PLC0415 - nox.paths is import-light, this module is not

    base = home if home is not None else nox_home()
    marker = _read_marker(base)
    if backend != "auto":
        chosen: str = backend
        if marker != chosen:
            _write_marker(base, chosen)
    elif marker is not None:
        chosen = marker
    else:
        chosen = "keyring" if keyring_usable() else "file"
        _write_marker(base, chosen)
    if chosen == "keyring" and not keyring_usable():
        return "unavailable"
    return chosen


def open_secret_store(backend: SecretBackend = "auto", *, home: Path | None = None) -> SecretStore:
    """The store every process uses, so the core, the CLI and the wizard agree on one place.

    Callers pass the configured `security.secret_store`; `open_configured_secret_store` does
    that for a process that has not loaded the configuration itself.
    """
    from nox.paths import nox_home  # noqa: PLC0415

    base = home if home is not None else nox_home()
    chosen = resolve_secret_backend(backend, home=base)
    if chosen == "unavailable":
        log.error("secrets.backend_unavailable", backend="keyring")
        return UnavailableSecretStore(
            "this installation keeps its secrets in the OS keyring, which is not available "
            "right now (no desktop session, or D-Bus/Secret Service missing)"
        )
    if chosen == "keyring":
        log.info("secrets.backend", backend="keyring")
        return KeyringSecretStore()
    from nox.security.file_secrets import FileSecretStore  # noqa: PLC0415

    log.info("secrets.backend", backend="file")
    return FileSecretStore(base)


def open_configured_secret_store() -> SecretStore:
    """`open_secret_store` with the backend from the user's configuration (CLI, wizard)."""
    from nox.core.config import load_config  # noqa: PLC0415 - heavy, and only this path needs it
    from nox.paths import resolve_config_paths  # noqa: PLC0415

    defaults, user = resolve_config_paths()
    return open_secret_store(load_config(defaults, user).security.secret_store)


# ---- PIN -----------------------------------------------------------------------------------------


class PinStatus(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    locked: bool = False
    locked_until: datetime | None = None
    remaining_attempts: int = 0
    reason: str = ""


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
        min_length: int = 4,
        prefer_argon2: bool = True,
        attempts: PinAttemptStore | None = None,
    ) -> None:
        self._store = store
        self._audit = SafeAuditLog(audit)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._lockout = timedelta(seconds=lockout_s)
        self._min_length = min_length
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
        """Whether a PIN exists. Fail-closed: a store that cannot be read counts as "set", so a
        gate that asks for the PIN keeps asking - and `verify_pin` then says why it cannot."""
        try:
            return self._store.get(PIN_SECRET_NAME) is not None
        except SecretStoreError as exc:
            log.error("security.pin_store_unreadable", error=str(exc))
            return True

    def is_locked(self) -> bool:
        state = self._attempts.load()
        if state.locked_until is None:
            return False
        if self._clock() >= state.locked_until:
            self._attempts.clear()
            return False
        return True

    def set_pin(self, pin: str, *, by: str = "user") -> None:
        if len(pin) < self._min_length:
            raise ValueError(f"PIN must have at least {self._min_length} characters")
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
        state = self._attempts.load()
        if self.is_locked():
            return PinStatus(
                ok=False,
                locked=True,
                locked_until=state.locked_until,
                remaining_attempts=0,
                reason="locked out",
            )
        try:
            stored = self._store.get(PIN_SECRET_NAME)
        except SecretStoreError as exc:
            return PinStatus(
                ok=False,
                remaining_attempts=self._remaining(state),
                reason=f"secret store unavailable: {exc}",
            )
        if stored is None:
            return PinStatus(
                ok=False, remaining_attempts=self._remaining(state), reason="no PIN set"
            )
        if stored.startswith("argon2id$") and self._argon2 is None:
            # Not "wrong PIN": the stored hash is fine and the entered PIN may be too. Saying so
            # is the difference between a user retyping forever and a user reinstalling a package.
            log.error("security.pin_backend_missing", reason=self._argon2_reason)
            return PinStatus(
                ok=False,
                remaining_attempts=self._remaining(state),
                reason=f"PIN hashing backend missing ({self._argon2_reason})",
            )
        if self._verify(stored, pin):
            self._attempts.clear()
            self._audit.append(actor=by, action="pin.verify", target="pin")
            self._upgrade_hash(stored, pin)
            return PinStatus(ok=True, remaining_attempts=self._max_attempts)
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
            ok=False, remaining_attempts=max(self._max_attempts - failed, 0), reason="wrong PIN"
        )

    # ---- hashing ---------------------------------------------------------------------------------

    def _remaining(self, state: PinAttemptState) -> int:
        return max(self._max_attempts - state.failed, 0)

    def _hash(self, pin: str) -> str:
        if self._argon2 is not None:
            return "argon2id$" + str(self._argon2.hash(pin))
        salt = _secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"

    def _upgrade_hash(self, stored: str, pin: str) -> None:
        """Re-hash a correctly entered PIN whose stored hash is weaker than what is available now:
        PBKDF2 from a start without Argon2id, or Argon2id with older parameters. The moment the
        PIN is entered is the only one at which that is possible."""
        if self._argon2 is None:
            return
        scheme, _, rest = stored.partition("$")
        if scheme == "argon2id":
            try:
                if not self._argon2.check_needs_rehash(rest):
                    return
            except Exception:  # noqa: BLE001 - an unreadable hash just verified; keep it
                return
        self._store.set(PIN_SECRET_NAME, self._hash(pin))
        self._audit.append(
            actor="core",
            action="pin.rehash",
            target="pin",
            details={"from": scheme, "to": "argon2id"},
        )
        log.info("security.pin_rehashed", previous=scheme)

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
