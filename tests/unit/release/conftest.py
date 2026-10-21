"""A throwaway release key per test - generated here, never stored, never a real release key."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nox.release.keys import TrustedKey, describe_public_key

COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass(frozen=True)
class ThrowawayKey:
    private: Ed25519PrivateKey
    trusted: TrustedKey


@pytest.fixture
def make_key() -> Callable[..., ThrowawayKey]:
    def make(not_after: str = "2999-12-31") -> ThrowawayKey:
        private = Ed25519PrivateKey.generate()
        key_id, spki = describe_public_key(private.public_key())
        return ThrowawayKey(
            private, TrustedKey(key_id=key_id, public_key=spki, not_after=not_after)
        )

    return make


@pytest.fixture
def release_key(make_key: Callable[..., ThrowawayKey]) -> ThrowawayKey:
    key: ThrowawayKey = make_key()
    return key
