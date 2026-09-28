"""SecurityChangeGate: a relaxing change needs the PIN, and "cannot read the PIN" is never
"no PIN"."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.boot.health import core_health_checks
from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.security.gate import PinRequiredError, SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager, SecretStoreUnavailableError


class UnreadableStore:
    """A credential store that is not there - a Linux session without a Secret Service."""

    def get(self, name: str) -> str | None:
        raise SecretStoreUnavailableError("no credential store is available")

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailableError("no credential store is available")

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailableError("no credential store is available")


def _gate(store: object, *, required: bool = True) -> SecurityChangeGate:
    return SecurityChangeGate(PinManager(store, prefer_argon2=False), required=required)  # type: ignore[arg-type]


async def test_no_pin_configured_gates_nothing() -> None:
    gate = _gate(InMemorySecretStore())
    assert gate.describe() == "off"
    await gate.require(None, action="privacy.set_mode", by="test")


async def test_a_configured_pin_is_required_and_checked() -> None:
    store = InMemorySecretStore()
    PinManager(store, prefer_argon2=False).set_pin("471108")
    gate = _gate(store)
    assert gate.describe() == "on"
    with pytest.raises(PinRequiredError):
        await gate.require(None, action="privacy.set_mode", by="test")
    await gate.require("471108", action="privacy.set_mode", by="test")


async def test_an_unreadable_credential_store_refuses_every_relaxing_change() -> None:
    gate = _gate(UnreadableStore())
    assert gate.is_required() is True
    assert gate.describe() == "unknown"
    for pin in (None, "471108"):
        with pytest.raises(PinRequiredError, match="cannot be read"):
            await gate.require(pin, action="privacy.set_mode", by="test")


async def test_the_setting_off_gates_nothing_even_without_a_store() -> None:
    gate = _gate(UnreadableStore(), required=False)
    assert gate.describe() == "off"
    await gate.require(None, action="privacy.set_mode", by="test")


async def test_health_reports_an_unreachable_credential_store() -> None:
    checks = core_health_checks(
        database=lambda: None,
        vault_dir=lambda: Path("."),
        workers=None,  # type: ignore[arg-type]
        voice_enabled=False,
        tokens=lambda: None,
        providers=lambda: [],
        secrets=lambda: UnreadableStore(),  # type: ignore[arg-type,return-value]
    )
    secrets = next(check for check in checks if check.name == "secrets")
    status, reason = await secrets.probe()
    assert status is HealthStatus.UNAVAILABLE
    assert "credential store" in reason


async def test_a_gate_refusal_names_its_reason_for_the_ui() -> None:
    """The dashboard shows "PIN nötig", "PIN falsch" or "gesperrt bis" from `details.reason`."""
    store = InMemorySecretStore()
    PinManager(store, prefer_argon2=False).set_pin("471108")
    gate = _gate(store)

    with pytest.raises(PinRequiredError) as missing:
        await gate.require(None, action="privacy.set", by="dashboard")
    assert missing.value.details == {"reason": "pin_required"}

    with pytest.raises(PinRequiredError) as wrong:
        await gate.require("000000", action="privacy.set", by="dashboard")
    assert wrong.value.details == {"reason": "pin_wrong", "remaining_attempts": 4}

    with pytest.raises(PinRequiredError) as unknown:
        await _gate(UnreadableStore()).require("471108", action="privacy.set", by="dashboard")
    assert unknown.value.details == {"reason": "store_unavailable"}


def _secrets_check(store: object) -> Check:
    checks = core_health_checks(
        database=lambda: None,
        vault_dir=lambda: Path("."),
        workers=None,  # type: ignore[arg-type]
        voice_enabled=False,
        tokens=lambda: None,
        providers=lambda: [],
        secrets=lambda: store,  # type: ignore[arg-type,return-value]
    )
    return next(check for check in checks if check.name == "secrets")


async def test_health_names_a_raw_value_stored_as_the_pin() -> None:
    """`nox secrets set nox/security/pin` used to leave a value no PIN can ever match."""
    store = InMemorySecretStore()
    store.set("nox/security/pin", "471108")

    status, reason = await _secrets_check(store).probe()

    assert status is HealthStatus.LIMITED
    assert "not a Nox PIN hash" in reason and "nox pin set" in reason
    assert "471108" not in reason


async def test_health_is_fine_with_a_real_pin_hash() -> None:
    store = InMemorySecretStore()
    PinManager(store).set_pin("471108")

    assert await _secrets_check(store).probe() == (HealthStatus.AVAILABLE, "ok")


def test_doctor_reports_the_pin_without_showing_it() -> None:
    from nox.entrypoints import pin_line  # noqa: PLC0415 - imports the whole core

    store = InMemorySecretStore()
    assert pin_line(store).startswith("[warn] security PIN: not set")
    store.set("nox/security/pin", "471108")
    assert pin_line(store) == (
        "[FAIL] security PIN: PIN entry is not a Nox PIN hash - set it again with `nox pin set`"
    )
    PinManager(store).set_pin("471108")
    assert pin_line(store) == "[ ok ] security PIN: set"
    unreadable = UnreadableStore()
    assert pin_line(unreadable).startswith("[warn] security PIN: unknown")  # type: ignore[arg-type]
