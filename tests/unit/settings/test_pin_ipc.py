"""`security.pin.*` from the dashboard: set, change, remove, and every way it is refused.

The PIN never appears in an answer, an error or the audit log; a refusal carries a stable
`details.reason` the dashboard translates, plus the facts it needs (attempts left, lockout end).
"""

from __future__ import annotations

import pytest

from nox.ipc.errors import (
    ERR_PERMISSION,
    ERR_RATE_LIMITED,
    ERR_UNAVAILABLE,
    ERR_VALIDATION,
    IpcError,
)
from nox.security.secrets import (
    PIN_SECRET_NAME,
    InMemorySecretStore,
    KeyringSecretStore,
    PinManager,
)
from nox.settings.pin_ipc import PinService
from tests.unit.settings.conftest import FakeAudit

PIN = "471108"
OTHER = "902211"


def _service(
    secrets: InMemorySecretStore, audit: FakeAudit, **kwargs: object
) -> tuple[PinService, PinManager]:
    pin = PinManager(secrets, audit=audit, prefer_argon2=False)
    return PinService(pin, audit=audit, **kwargs), pin  # type: ignore[arg-type]


async def test_the_first_pin_needs_nothing_and_is_stored_as_a_hash(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service, pin = _service(secrets, audit)

    assert await service.set(PIN, current_pin=None, by="dashboard") == {
        "ok": True,
        "state": "valid",
    }

    stored = secrets.get(PIN_SECRET_NAME) or ""
    assert stored.startswith("pbkdf2_sha256$") and PIN not in stored
    assert pin.verify_pin(PIN).ok
    assert any(e["action"] == "pin.set" for e in audit.entries)
    assert PIN not in audit.blob()


async def test_a_too_short_pin_is_refused_with_the_minimum(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service, _pin = _service(secrets, audit)

    with pytest.raises(IpcError) as exc:
        await service.set("1234", current_pin=None, by="dashboard")

    assert exc.value.code == ERR_VALIDATION
    assert exc.value.details["reason"] == "too_short"
    assert exc.value.details["min_length"] == 6
    assert secrets.get(PIN_SECRET_NAME) is None
    assert "1234" not in repr(exc.value.to_payload())


async def test_changing_the_pin_needs_the_current_one(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service, pin = _service(secrets, audit)
    await service.set(PIN, current_pin=None, by="dashboard")

    with pytest.raises(IpcError) as missing:
        await service.set(OTHER, current_pin=None, by="dashboard")
    assert missing.value.code == ERR_PERMISSION
    assert missing.value.details == {"reason": "pin_required"}

    with pytest.raises(IpcError) as wrong:
        await service.set(OTHER, current_pin="000000", by="dashboard")
    assert wrong.value.code == ERR_PERMISSION
    assert wrong.value.details == {"reason": "pin_wrong", "remaining_attempts": 4}

    assert pin.verify_pin(PIN).ok  # nothing changed
    await service.set(OTHER, current_pin=PIN, by="dashboard")
    assert pin.verify_pin(OTHER).ok and not pin.verify_pin(PIN).ok
    refusals = [e for e in audit.entries if e["action"] == "pin.set" and e["decision"] == "deny"]
    assert [e["details"]["reason"] for e in refusals] == ["pin_required", "pin_wrong"]


async def test_wrong_current_pins_lock_the_change_out(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service, _pin = _service(secrets, audit, max_calls=100)
    await service.set(PIN, current_pin=None, by="dashboard")
    for _ in range(4):
        with pytest.raises(IpcError):
            await service.set(OTHER, current_pin="000000", by="dashboard")

    with pytest.raises(IpcError) as fifth:
        await service.set(OTHER, current_pin="000000", by="dashboard")
    assert fifth.value.details["reason"] == "locked"
    assert "locked_until" in fifth.value.details

    # While locked, even the right PIN changes nothing.
    with pytest.raises(IpcError) as locked:
        await service.set(OTHER, current_pin=PIN, by="dashboard")
    assert locked.value.details["reason"] == "locked"
    assert service.status()["locked_until"] is not None


async def test_removing_the_pin_needs_the_current_one(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    service, pin = _service(secrets, audit)

    with pytest.raises(IpcError) as nothing:
        await service.clear(current_pin=None, by="dashboard")
    assert nothing.value.code == ERR_VALIDATION and nothing.value.details["reason"] == "not_set"

    await service.set(PIN, current_pin=None, by="dashboard")
    with pytest.raises(IpcError) as missing:
        await service.clear(current_pin=None, by="dashboard")
    assert missing.value.details["reason"] == "pin_required"
    with pytest.raises(IpcError):
        await service.clear(current_pin="000000", by="dashboard")
    assert pin.is_set()

    assert await service.clear(current_pin=PIN, by="dashboard") == {
        "ok": True,
        "state": "not_set",
    }
    assert not pin.is_set()


async def test_a_raw_value_under_the_pin_name_is_reported_and_never_accepted(
    secrets: InMemorySecretStore, audit: FakeAudit
) -> None:
    """What `nox secrets set nox/security/pin` used to leave behind: fail closed, name the fix."""
    secrets.set(PIN_SECRET_NAME, PIN)
    service, pin = _service(secrets, audit)

    status = service.status()
    assert status["state"] == "invalid" and status["configured"] is True

    verification = pin.verify_pin(PIN)
    assert not verification.ok
    assert verification.code == "invalid_entry"
    assert "nox pin set" in verification.reason
    assert verification.remaining_attempts == 5  # a broken entry is not a wrong guess

    with pytest.raises(IpcError) as replace:
        await service.set(OTHER, current_pin=PIN, by="dashboard")
    assert replace.value.code == ERR_PERMISSION
    assert replace.value.details["reason"] == "invalid_entry"
    with pytest.raises(IpcError) as clear:
        await service.clear(current_pin=PIN, by="dashboard")
    assert clear.value.details["reason"] == "invalid_entry"
    assert secrets.get(PIN_SECRET_NAME) == PIN  # untouched: only the local CLI may repair it


async def test_pin_changes_are_rate_limited(secrets: InMemorySecretStore, audit: FakeAudit) -> None:
    now = [0.0]
    service, _pin = _service(secrets, audit, max_calls=2, window_s=60.0, clock=lambda: now[0])
    await service.set(PIN, current_pin=None, by="dashboard")
    with pytest.raises(IpcError):
        await service.set(OTHER, current_pin="000000", by="dashboard")

    with pytest.raises(IpcError) as limited:
        await service.set(OTHER, current_pin=PIN, by="dashboard")
    assert limited.value.code == ERR_RATE_LIMITED

    now[0] = 61.0
    await service.set(OTHER, current_pin=PIN, by="dashboard")


async def test_an_unreachable_credential_store_fails_closed(audit: FakeAudit) -> None:
    class NoKeyringError(RuntimeError):
        pass

    class NoBackend:
        def get_password(self, service: str, name: str) -> str | None:
            raise NoKeyringError("no backend")

        def set_password(self, service: str, name: str, value: str) -> None:
            raise NoKeyringError("no backend")

    pin = PinManager(KeyringSecretStore(backend=NoBackend()), prefer_argon2=False)
    service = PinService(pin, audit=audit)

    status = service.status()
    assert status["state"] == "unavailable" and status["configured"] is True

    with pytest.raises(IpcError) as exc:
        await service.set(PIN, current_pin=None, by="dashboard")
    assert exc.value.code == ERR_UNAVAILABLE
    assert exc.value.details["reason"] == "store_unavailable"
