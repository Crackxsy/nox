"""Signed releases (docs/RELEASES.md): the manifest format, the trusted release keys, and the
verification every update path runs before it uses anything it downloaded.

`nox.release` imports only the standard library and `cryptography`, so the release workflow can
sign and an installer can verify without the rest of Nox.
"""

from __future__ import annotations

from nox.release import trusted_keys as _trusted
from nox.release.keys import TrustedKey

__all__ = ["trusted_release_keys"]


def trusted_release_keys() -> tuple[TrustedKey, ...]:
    """The release keys this version trusts (read at call time, so tests can replace them)."""
    return _trusted.TRUSTED_KEYS
