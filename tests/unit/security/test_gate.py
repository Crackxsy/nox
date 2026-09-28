"""SecurityChangeGate: a relaxing change needs the PIN, and "cannot read the PIN" is never
"no PIN"."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.boot.health import core_health_checks
from nox.core.events import HealthStatus
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
    PinManager(store, prefer_argon2=False).set_pin("4711")
    gate = _gate(store)
    assert gate.describe() == "on"
    with pytest.raises(PinRequiredError):
        await gate.require(None, action="privacy.set_mode", by="test")
    await gate.require("4711", action="privacy.set_mode", by="test")


async def test_an_unreadable_credential_store_refuses_every_relaxing_change() -> None:
    gate = _gate(UnreadableStore())
    assert gate.is_required() is True
    assert gate.describe() == "unknown"
    for pin in (None, "4711"):
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
