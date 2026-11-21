"""The release keys this version of Nox trusts - the root of trust for every update.

A release manifest (`release-manifest.json`) counts only when it carries a valid Ed25519 signature
by one of the keys below that has not expired. The list ships *inside* each release, so the version
that is installed decides which keys may sign the next one; that is also how a key is rotated: a
release signed by the old key adds the new one, the next release is signed by the new key
(docs/RELEASES.md, "Rotation").

**Empty until the owner has created the release key.** Then every verifier says "release signing
is not configured" instead of pretending to check something. To configure it, the owner (and only
the owner - the private key never enters this repository) creates the key offline and adds one
entry here **and the identical line to `TRUSTED_RELEASE_KEYS` in `scripts/install-linux.sh`**
(a test keeps the two equal):

    TrustedKey(
        key_id="<16 hex characters printed by `nox release keys --describe <public key>`>",
        public_key="<the base64 line between the PEM markers of `openssl pkey -pubout`>",
        not_after="YYYY-MM-DD",  # last day (UTC) a manifest signed by this key is accepted
    ),

Data only: no code runs from here and nothing is read from the network.
"""

from __future__ import annotations

from nox.release.keys import TrustedKey

__all__ = ["TRUSTED_KEYS"]

TRUSTED_KEYS: tuple[TrustedKey, ...] = (
    # CI signing key: the secret half lives only in the GitHub environment `release-signing`.
    TrustedKey(
        key_id="306d7c114db7e216",
        public_key="MCowBQYDK2VwAyEAU+qRJ6vGP2JUXp8PFHXGKDjtD1yDmPAZj1Y7n7xth+o=",
        not_after="2028-11-30",
    ),
    # Recovery key: offline only, never in CI; signs the rotation release if the CI key leaks.
    TrustedKey(
        key_id="4735ec2541181aed",
        public_key="MCowBQYDK2VwAyEA9+aGl10Xs1YC3vxbFemdrxZCBeBUI5IBd83B8iy+2AA=",
        not_after="2031-11-30",
    ),
)
