"""Write (and, when the release key is available, sign) the manifest of one release.

Used by .github/workflows/release-assets.yml; docs/RELEASES.md explains the whole procedure.

    python scripts/sign_release.py --tag v0.7.0 --commit <40 hex> --out signed/ FILE...

The private key is read from the environment variable NOX_RELEASE_SIGNING_KEY or, with
--key-stdin, from standard input - never from the command line, which other processes and CI logs
can see. It is removed from this process's environment as soon as it is read, it is never printed,
and errors about it never quote it.

Without a key the result depends on --allow-unsigned: with it, an UNSIGNED manifest is written
(for inspection only - the workflow refuses to publish it) together with a loud warning; without
it, the script fails. A key that this release itself does not trust (not in
src/nox/release/trusted_keys.py, or expired) is refused too: a signature nobody can check would
only pretend.

Before anything is written, the signature is verified exactly the way an installation will verify
it. Exit status: 0 done (signed, or unsigned with --allow-unsigned), 1 refused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The checkout's own code - the release being signed - not whatever `nox` is installed elsewhere.
sys.path.insert(0, str(REPO / "src"))

from nox.release import trusted_release_keys  # noqa: E402
from nox.release.keys import describe_public_key  # noqa: E402
from nox.release.manifest import (  # noqa: E402
    MANIFEST_NAME,
    SIGNATURE_NAME,
    ManifestError,
    asset_for_file,
    build_manifest,
)
from nox.release.sign import SigningKeyError, load_signing_key, sign_manifest  # noqa: E402
from nox.release.verify import ReleaseVerificationError, verify_release  # noqa: E402

KEY_VARIABLE = "NOX_RELEASE_SIGNING_KEY"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PROTOCOL = re.compile(r"^(\d+)-(\d+)$")


def _github_output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _annotate(level: str, title: str, message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{message}")
    else:
        print(f"{level.upper()}: {title}: {message}", file=sys.stderr)


def _read_key(from_stdin: bool) -> str:
    """The key text, or "" when none was provided. The variable is cleared either way, so no
    child process or later step of this one inherits it."""
    from_env = os.environ.pop(KEY_VARIABLE, "")
    if from_stdin:
        return sys.stdin.read()
    return from_env


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.7.0")
    parser.add_argument("--commit", required=True, help="the full commit id the tag points to")
    parser.add_argument("--out", required=True, type=Path, help="folder for the manifest")
    parser.add_argument("--key-stdin", action="store_true", help="read the private key from stdin")
    parser.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="without a key, write an unsigned manifest (never published) instead of failing",
    )
    parser.add_argument("--node-protocol", help="supported node protocol range, e.g. 1-2")
    parser.add_argument("--min-node-version", help="oldest node version this core works with")
    parser.add_argument("files", nargs="+", type=Path, help="the files the release publishes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    key_text = _read_key(args.key_stdin)
    if not _COMMIT.match(args.commit):
        print("refused: --commit must be a full 40-character commit id", file=sys.stderr)
        return 1
    protocol: tuple[int, int] | None = None
    if args.node_protocol:
        match = _PROTOCOL.match(args.node_protocol)
        if match is None:
            print("refused: --node-protocol must look like 1-2", file=sys.stderr)
            return 1
        protocol = (int(match.group(1)), int(match.group(2)))

    trusted = trusted_release_keys()
    try:
        assets = [asset_for_file(path) for path in args.files]
        manifest = build_manifest(
            tag=args.tag,
            source_commit=args.commit,
            assets=assets,
            next_keys=trusted,
            node_protocol=protocol,
            min_node_version=args.min_node_version,
        )
    except (ManifestError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / MANIFEST_NAME
    signature_path = args.out / SIGNATURE_NAME
    signature_path.unlink(missing_ok=True)

    if not key_text.strip():
        if not args.allow_unsigned:
            print(f"refused: no signing key ({KEY_VARIABLE} is not set)", file=sys.stderr)
            return 1
        manifest_path.write_bytes(manifest)
        _annotate(
            "warning",
            "Release not signed",
            f"{KEY_VARIABLE} is not configured, so {args.tag} gets no signed manifest and "
            "installations that trust a release key will refuse it. The owner sets the key up "
            "as described in docs/RELEASES.md.",
        )
        _github_output("signed", "false")
        return 0

    try:
        key = load_signing_key(key_text)
    except SigningKeyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        del key_text
    key_id, public_key = describe_public_key(key.public_key())
    if not any(entry.key_id == key_id for entry in trusted):
        print(
            f"refused: the signing key {key_id} is not in src/nox/release/trusted_keys.py of "
            f"{args.tag}. Add its public key ({public_key}) there and to scripts/install-linux.sh "
            "first - docs/RELEASES.md, 'Setting up the release key'.",
            file=sys.stderr,
        )
        return 1

    signature = sign_manifest(manifest, key)
    try:
        # Exactly the check an installation runs - including the key's expiry.
        verify_release(
            manifest,
            signature,
            trusted_keys=trusted,
            expect_tag=args.tag,
            expect_commit=args.commit,
        )
    except ReleaseVerificationError as exc:
        print(f"refused: the new signature does not verify: {exc}", file=sys.stderr)
        return 1
    manifest_path.write_bytes(manifest)
    signature_path.write_bytes(signature)
    print(f"Signed {args.tag} with release key {key_id}: {len(assets)} files.")
    _github_output("signed", "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
