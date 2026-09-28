"""Secrets and PIN: naming, in-memory/keyring stores, values never logged, lockout after 5
failures."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import structlog

import nox.security.secrets as secrets_mod
from nox.security.audit import SqliteAuditLog
from nox.security.secrets import (
    PIN_SECRET_NAME,
    InMemorySecretStore,
    KeyringSecretStore,
    PinCode,
    PinEntryState,
    PinManager,
    PinPolicyError,
    SecretNameError,
    SecretStoreUnavailableError,
    looks_like_pin_hash,
)

from .conftest import MutableClock

SECRET = "sk-live-VERY-SECRET-VALUE-0123456789"


class RecordingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def _log(*args: Any, **kwargs: Any) -> None:
            self.calls.append((level, args, kwargs))

        return _log

    def dump(self) -> str:
        return json.dumps(self.calls, default=str)


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise _PasswordDeleteError()
        del self.store[(service, username)]


class _PasswordDeleteError(Exception):
    pass


_PasswordDeleteError.__name__ = "PasswordDeleteError"


def test_in_memory_store_roundtrip_and_name_validation() -> None:
    store = InMemorySecretStore()
    store.set("nox/twitch/oauth_token", SECRET)
    assert store.get("nox/twitch/oauth_token") == SECRET
    store.delete("nox/twitch/oauth_token")
    assert store.get("nox/twitch/oauth_token") is None
    store.delete("nox/twitch/oauth_token")  # idempotent
    for bad in ("twitch/token", "nox/twitch", "NOX/twitch/token", "nox/twitch/to ken", ""):
        with pytest.raises(SecretNameError):
            store.set(bad, "x")
    assert SECRET not in repr(store)


def test_keyring_store_uses_service_nox_and_full_name_as_username() -> None:
    kr = FakeKeyring()
    store = KeyringSecretStore(backend=kr)
    store.set("nox/telegram/bot_token", SECRET)
    assert kr.store == {("nox", "nox/telegram/bot_token"): SECRET}
    assert store.get("nox/telegram/bot_token") == SECRET
    store.delete("nox/telegram/bot_token")
    store.delete("nox/telegram/bot_token")  # missing -> PasswordDeleteError swallowed
    assert store.get("nox/telegram/bot_token") is None
    assert SECRET not in repr(store)


def test_secret_values_never_appear_in_logs(
    monkeypatch: pytest.MonkeyPatch, audit: SqliteAuditLog
) -> None:
    rec = RecordingLogger()
    monkeypatch.setattr(secrets_mod, "log", rec)
    kr = FakeKeyring()
    with structlog.testing.capture_logs() as captured:
        store = KeyringSecretStore(backend=kr)
        store.set("nox/openai/api_key", SECRET)
        store.get("nox/openai/api_key")
        store.delete("nox/openai/api_key")
        pin = PinManager(InMemorySecretStore(), audit=audit, prefer_argon2=False)
        pin.set_pin("987654")
        pin.verify_pin("987654")
        pin.verify_pin("000000")
    assert rec.calls, "logger was used"
    assert SECRET not in rec.dump() and "987654" not in rec.dump() and "000000" not in rec.dump()
    assert SECRET not in json.dumps(captured, default=str) and "987654" not in json.dumps(
        captured, default=str
    )
    # Exclude the hash-chain fields: GENESIS_HASH ("0" * 64) trivially contains "000000" and is
    # unrelated to secret leakage - the actual leak surface is the semantic fields below.
    entry_dumps = [
        {k: v for k, v in e.model_dump().items() if k not in ("hash", "prev_hash")}
        for e in audit.entries()
    ]
    audit_dump = json.dumps(entry_dumps, default=str)
    audit_dump += json.dumps([audit.details(e.seq) for e in audit.entries()])
    assert "987654" not in audit_dump and "000000" not in audit_dump


def test_pin_set_verify_and_lockout(clock: MutableClock, audit: SqliteAuditLog) -> None:
    pin = PinManager(InMemorySecretStore(), audit=audit, clock=clock, prefer_argon2=False)
    assert not pin.is_set()
    assert pin.verify_pin("123456").reason == "no PIN set"
    with pytest.raises(ValueError):
        pin.set_pin("12")
    pin.set_pin("123456")
    assert pin.is_set() and pin.algorithm == "pbkdf2_sha256"
    assert pin.verify_pin("123456").ok
    for i in range(4):
        status = pin.verify_pin("000000")
        assert not status.ok and not status.locked and status.remaining_attempts == 4 - i
    status = pin.verify_pin("000000")
    assert status.locked and status.remaining_attempts == 0 and status.locked_until is not None
    assert not pin.verify_pin("123456").ok and pin.is_locked()  # right PIN is refused while locked
    clock.advance(15 * 60 - 1)
    assert pin.is_locked()
    clock.advance(2)
    assert not pin.is_locked() and pin.verify_pin("123456").ok
    actions = [e.action for e in audit.entries() if e.actor == "user"]
    assert "pin.lockout" in actions and actions.count("pin.verify") >= 6


def test_pin_hash_format_and_argon2_fallback_report() -> None:
    store = InMemorySecretStore()
    pin = PinManager(store, prefer_argon2=False)
    pin.set_pin("471108")
    stored = store.get(PIN_SECRET_NAME)
    assert (
        stored is not None and stored.startswith("pbkdf2_sha256$600000$") and "471108" not in stored
    )
    auto = PinManager(store)
    assert auto.verify_pin("471108").ok  # verifies pbkdf2 regardless of argon2 presence
    assert auto.algorithm in ("argon2id", "pbkdf2_sha256")
    if auto.algorithm == "argon2id":
        auto.set_pin("471108")
        assert (store.get(PIN_SECRET_NAME) or "").startswith("argon2id$")
        assert auto.verify_pin("471108").ok and not auto.verify_pin("471109").ok
    store.set(PIN_SECRET_NAME, "argon2id$garbage")
    assert not PinManager(store, prefer_argon2=False).verify_pin("471108").ok
    store.set(PIN_SECRET_NAME, "pbkdf2_sha256$broken")
    assert not PinManager(store, prefer_argon2=False).verify_pin("471108").ok
    pin.clear_pin()
    assert not pin.is_set()


def test_a_missing_credential_store_is_an_error_never_a_missing_secret() -> None:
    """A PIN that cannot be read must not look like no PIN at all (fail closed)."""

    class NoKeyringError(RuntimeError):
        pass

    class NoBackend:
        def get_password(self, service: str, name: str) -> str | None:
            raise NoKeyringError("No recommended backend was available.")

        def set_password(self, service: str, name: str, value: str) -> None:
            raise NoKeyringError("No recommended backend was available.")

    store = KeyringSecretStore(backend=NoBackend())
    with pytest.raises(SecretStoreUnavailableError, match="Secret Service"):
        store.get("nox/security/pin")
    with pytest.raises(SecretStoreUnavailableError):
        store.set("nox/telegram/bot_token", SECRET)
    with pytest.raises(SecretStoreUnavailableError):
        PinManager(store).is_set()


# ---- Argon2id, re-hashing, broken entries, concurrency ---------------------------------------


def test_argon2id_is_the_scheme_in_use() -> None:
    """`argon2-cffi` is a dependency: SECURITY.md promises Argon2id, and that is now what runs."""
    store = InMemorySecretStore()
    pin = PinManager(store)

    pin.set_pin("471108")

    assert pin.algorithm == "argon2id" and pin.hash_backend_reason == ""
    assert (store.get(PIN_SECRET_NAME) or "").startswith("argon2id$$argon2id$")
    assert pin.verify_pin("471108").ok and not pin.verify_pin("471109").ok


def test_a_pbkdf2_hash_is_rehashed_to_argon2id_on_the_next_success(audit: SqliteAuditLog) -> None:
    store = InMemorySecretStore()
    PinManager(store, prefer_argon2=False).set_pin("471108")  # written by an older version
    assert (store.get(PIN_SECRET_NAME) or "").startswith("pbkdf2_sha256$")
    pin = PinManager(store, audit=audit)

    assert not pin.verify_pin("471109").ok
    assert (store.get(PIN_SECRET_NAME) or "").startswith("pbkdf2_sha256$")  # not on a failure

    assert pin.verify_pin("471108").ok
    upgraded = store.get(PIN_SECRET_NAME) or ""
    assert upgraded.startswith("argon2id$") and "471108" not in upgraded
    assert pin.verify_pin("471108").ok  # the same PIN still works, now against Argon2id
    assert "pin.rehash" in [e.action for e in audit.entries()]


def test_a_raw_value_under_the_pin_name_is_reported_not_counted() -> None:
    """`nox secrets set nox/security/pin` used to store the PIN itself: say so, never match it."""
    store = InMemorySecretStore()
    store.set(PIN_SECRET_NAME, "471108")
    pin = PinManager(store, prefer_argon2=False)

    assert pin.entry_state() is PinEntryState.INVALID
    for _ in range(6):
        status = pin.verify_pin("471108")
        assert not status.ok and status.code is PinCode.INVALID_ENTRY
        assert "nox pin set" in status.reason
    assert not pin.is_locked()  # a broken entry is not a guess


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("argon2id$$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA", True),
        ("pbkdf2_sha256$600000$00ff$abcd", True),
        ("471108", False),
        ("argon2id$garbage", False),
        ("pbkdf2_sha256$broken", False),
        ("", False),
    ],
)
def test_only_hashes_this_module_writes_count_as_a_pin_entry(value: str, valid: bool) -> None:
    assert looks_like_pin_hash(value) is valid


def test_a_new_pin_needs_six_to_sixty_four_characters() -> None:
    pin = PinManager(InMemorySecretStore(), prefer_argon2=False)
    with pytest.raises(PinPolicyError) as short:
        pin.set_pin("47110")
    assert short.value.code == "too_short"
    with pytest.raises(PinPolicyError) as long:
        pin.set_pin("4" * 65)
    assert long.value.code == "too_long"
    assert not pin.is_set()


async def test_concurrent_guesses_are_counted_one_by_one() -> None:
    """Many requests at once used to read the same failure count: a burst was one "attempt"."""
    pin = PinManager(InMemorySecretStore(), prefer_argon2=False)
    pin.set_pin("471108")

    results = await asyncio.gather(*(pin.verify_pin_async("000000") for _ in range(8)))

    codes = sorted(r.code.value for r in results)
    assert codes.count("pin_wrong") == 4
    assert codes.count("locked") == 4
    assert pin.is_locked()
