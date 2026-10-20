"""The PIN a paired device must give (`nox.security.device_pin.DevicePinGuard`)."""

from __future__ import annotations

import pytest

from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.security.device_pin import DEFAULT_PIN_FAILURES, DevicePinGuard
from nox.security.gate import SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager

PIN = "4711"


def ctx(role: str, client_id: str) -> RequestContext:
    envelope = Envelope(
        kind=Kind.REQUEST,
        name="lan.pair.start",
        src=Source(role=role, id=client_id),  # type: ignore[arg-type]
    )
    return RequestContext(client_id=client_id, role=role, request=envelope)


def gate(pin: str | None) -> SecurityChangeGate:
    manager = PinManager(InMemorySecretStore(), prefer_argon2=False)
    if pin:
        manager.set_pin(pin, by="test")
    return SecurityChangeGate(manager, required=True)


class Revoked:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, device_id: str, reason: str) -> None:
        self.calls.append((device_id, reason))


DEVICE = ctx("device", "device:abc")


async def reason_of(guard: DevicePinGuard, pin: str | None) -> str:
    with pytest.raises(IpcError) as refused:
        await guard.require(DEVICE, pin, "lan.pair")
    assert refused.value.code == ERR_PERMISSION
    return str(refused.value.details["reason"])


async def test_local_clients_pass_untouched() -> None:
    guard = DevicePinGuard(lambda: gate(PIN), Revoked())
    for role in ("shell", "dashboard"):
        await guard.require(ctx(role, f"{role}:x"), None, "lan.pair")


async def test_without_a_pin_a_device_is_refused_outright() -> None:
    assert await reason_of(DevicePinGuard(lambda: gate(None), Revoked()), PIN) == "pin_not_set"


async def test_a_device_needs_the_right_pin() -> None:
    revoked = Revoked()
    guard = DevicePinGuard(lambda: gate(PIN), revoked)
    assert await reason_of(guard, None) == "pin_required"
    assert await reason_of(guard, "0000") == "pin_wrong"
    await guard.require(DEVICE, PIN, "lan.pair")
    assert revoked.calls == []


async def test_a_missing_pin_is_not_counted_as_a_wrong_one() -> None:
    revoked = Revoked()
    guard = DevicePinGuard(lambda: gate(PIN), revoked)
    for _ in range(DEFAULT_PIN_FAILURES + 2):
        assert await reason_of(guard, None) == "pin_required"
    assert guard.failures("abc") == 0 and revoked.calls == []


async def test_repeated_wrong_pins_remove_the_device_before_the_pin_locks() -> None:
    revoked = Revoked()
    shared = gate(PIN)
    guard = DevicePinGuard(lambda: shared, revoked)
    reasons = [await reason_of(guard, "0000") for _ in range(DEFAULT_PIN_FAILURES)]
    assert reasons == ["pin_wrong"] * (DEFAULT_PIN_FAILURES - 1) + ["removed"]
    assert revoked.calls == [("abc", "wrong_pin")]
    assert DEFAULT_PIN_FAILURES < 5  # below the PIN's own lockout
    # The owner's PIN still works: the thief could not lock it.
    await guard.require(ctx("device", "device:other"), PIN, "lan.pair")


async def test_a_right_pin_resets_the_count() -> None:
    revoked = Revoked()
    guard = DevicePinGuard(lambda: gate(PIN), revoked)
    await reason_of(guard, "0000")
    await reason_of(guard, "1111")
    await guard.require(DEVICE, PIN, "lan.pair")
    assert guard.failures("abc") == 0
    await reason_of(guard, "0000")
    assert revoked.calls == []


async def test_counts_are_per_device() -> None:
    revoked = Revoked()
    guard = DevicePinGuard(lambda: gate(PIN), revoked, limit=2)
    await reason_of(guard, "0000")
    with pytest.raises(IpcError):
        await guard.require(ctx("device", "device:other"), "0000", "lan.pair")
    assert revoked.calls == []
