"""Write (and, when the release key is given, sign) the manifest of one release.

Used by .github/workflows/release-assets.yml; docs/RELEASES.md explains the whole procedure.

    printf '%s\\n' "$KEY" | python -I -S scripts/sign_release.py --key-stdin --lib <dir> \\
        --tag v0.7.0 --commit <40 hex> --out signed/ FILE...

**Self-contained on purpose.** This process holds the release key, so it imports nothing but the
standard library and `cryptography` - not the `nox` package, not any other dependency. It is meant
to run with `python -I -S` (no site-packages, no .pth files, no environment variables, no current
folder on the path) and finds `cryptography` only in `--lib`, a folder that holds nothing else
(scripts/signing-requirements.txt, installed with hashes). The manifest it writes is the format
`nox.release` verifies; tests/unit/release/test_sign_script.py checks both against each other.

The private key - an unencrypted PKCS#8 PEM block, as `openssl genpkey -algorithm ed25519` writes
it - is read from standard input with --key-stdin and from nowhere else: never from the command
line, which other processes and CI logs can see, and not from an environment variable, which
every child process would inherit. It is never printed, and errors about it never quote it.

Without a key the result depends on --allow-unsigned: with it, an UNSIGNED manifest is written
(for inspection only - the workflow refuses to publish it) together with a loud warning; without
it, the script fails. A key that this release itself does not trust (not in --trust-list, by
default src/nox/release/trusted_keys.py, or expired) is refused too: a signature nobody can check
would only pretend. Before anything is written, the new signature is verified.

Exit status: 0 done (signed, or unsigned with --allow-unsigned), 1 refused.
"""

from __future__ import annotations

import argparse
import ast
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TRUST_LIST = REPO / "src" / "nox" / "release" / "trusted_keys.py"
MANIFEST_NAME = "release-manifest.json"
SIGNATURE_NAME = "release-manifest.json.sig"
# The formats of nox.release (manifest.py, verify.py); a test compares them.
MANIFEST_SCHEMA = "nox-release-manifest/1"
SIGNATURE_SCHEMA = "nox-release-signature/1"
PROJECT = "Crackxsy/nox"

_VERSION = re.compile(
    r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_KEY_ID = re.compile(r"^[0-9a-f]{16}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROTOCOL = re.compile(r"^(\d+)-(\d+)$")
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class RefusedError(Exception):
    """Stop with this message (never containing key material)."""


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


def _key_id(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def read_trust_list(path: Path) -> list[dict[str, str]]:
    """The TrustedKey(...) entries of trusted_keys.py, read as data (parsed, never executed)."""
    tree = ast.parse(path.read_text("utf-8"), str(path))
    value: ast.expr | None = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "TRUSTED_KEYS":
                value = node.value
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TRUSTED_KEYS" for target in node.targets
        ):
            value = node.value
    if not isinstance(value, ast.Tuple):
        raise RefusedError(f"{path}: TRUSTED_KEYS = ( ... ) not found")
    entries = []
    for element in value.elts:
        if not (
            isinstance(element, ast.Call)
            and isinstance(element.func, ast.Name)
            and element.func.id == "TrustedKey"
            and not element.args
        ):
            raise RefusedError(f"{path}: every entry must be TrustedKey(key_id=..., ...)")
        fields = {}
        for keyword in element.keywords:
            if not isinstance(keyword.value, ast.Constant) or not isinstance(
                keyword.value.value, str
            ):
                raise RefusedError(f"{path}: TrustedKey fields must be plain strings")
            fields[str(keyword.arg)] = keyword.value.value
        if set(fields) != {"key_id", "public_key", "not_after"}:
            raise RefusedError(f"{path}: TrustedKey needs key_id, public_key and not_after")
        der = base64.b64decode(fields["public_key"], validate=True)
        if (
            not _KEY_ID.match(fields["key_id"])
            or not _DAY.match(fields["not_after"])
            or len(der) != 44
            or not der.startswith(_SPKI_PREFIX)
            or _key_id(der[12:]) != fields["key_id"]
        ):
            raise RefusedError(f"{path}: entry {fields['key_id'][:16]!r} is malformed")
        entries.append(fields)
    return entries


def _asset(path: Path) -> dict[str, Any]:
    if not _ASSET_NAME.match(path.name) or ".." in path.name:
        raise RefusedError(f"{path.name[:60]!r} is not a plain file name")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"name": path.name, "size": size, "sha256": digest.hexdigest()}


def build_manifest(
    *,
    tag: str,
    commit: str,
    files: list[Path],
    next_keys: list[dict[str, str]],
    node_protocol: tuple[int, int] | None,
    min_node_version: str | None,
    created: dt.datetime | None = None,
) -> bytes:
    """The manifest's bytes, in exactly the layout nox.release.manifest.build_manifest writes."""
    if not tag.startswith("v") or not _VERSION.match(tag[1:]):
        raise RefusedError(f"not a release tag: {tag[:40]!r}")
    if not _COMMIT.match(commit):
        raise RefusedError("--commit must be a full 40-character commit id")
    if min_node_version is not None and not _VERSION.match(min_node_version):
        raise RefusedError("--min-node-version must be a release version")
    if node_protocol is not None and not 0 < node_protocol[0] <= node_protocol[1]:
        raise RefusedError("--node-protocol must be a range min-max")
    version = tag[1:]
    assets = sorted((_asset(path) for path in files), key=lambda asset: asset["name"])
    if len({asset["name"] for asset in assets}) != len(assets):
        raise RefusedError("a file is given twice")
    stamp = (created or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    document = {
        "schema": MANIFEST_SCHEMA,
        "project": PROJECT,
        "version": version,
        "tag": tag,
        "channel": "prerelease" if "-" in version else "stable",
        "created": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": commit,
        "node_protocol": (
            None if node_protocol is None else {"min": node_protocol[0], "max": node_protocol[1]}
        ),
        "min_node_version": min_node_version,
        "next_keys": [
            {"key_id": k["key_id"], "public_key": k["public_key"], "not_after": k["not_after"]}
            for k in next_keys
        ],
        "assets": assets,
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def sign(manifest: bytes, key_text: str, trusted: list[dict[str, str]], tag: str) -> bytes:
    """The signature file. Refuses an unusable, untrusted or expired key; self-verifies."""
    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415 - only from --lib
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PrivateKey,
    )

    try:
        key = serialization.load_pem_private_key(key_text.strip().encode("ascii", "replace"), None)
    except TypeError:
        raise RefusedError(
            "the signing key is encrypted; CI needs the unencrypted PKCS#8 PEM"
        ) from None
    except ValueError:
        # `from None`: the parser's own exception could carry fragments of the input.
        raise RefusedError("the signing key is not a PEM private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise RefusedError("the signing key is not an Ed25519 key")
    public = key.public_key()
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    spki = base64.b64encode(
        public.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")
    key_id = _key_id(raw)
    entry = next((k for k in trusted if k["key_id"] == key_id), None)
    if entry is None:
        raise RefusedError(
            f"the signing key {key_id} is not in the trust list of {tag}. Add its public key "
            f"({spki}) to src/nox/release/trusted_keys.py and scripts/install-linux.sh first - "
            "docs/RELEASES.md, 'Setting up the release key'."
        )
    if dt.datetime.now(dt.UTC).date().isoformat() > entry["not_after"]:
        raise RefusedError(f"the signing key {key_id} expired on {entry['not_after']}")
    signature = key.sign(manifest)
    try:
        public.verify(signature, manifest)
    except InvalidSignature:  # pragma: no cover - cannot happen with a sound library
        raise RefusedError("the new signature does not verify") from None
    document = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "ed25519",
        "key_id": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write and sign a Nox release manifest.")
    parser.add_argument("--tag", required=True, help="the release tag, e.g. v0.7.0")
    parser.add_argument("--commit", required=True, help="the full commit id the tag points to")
    parser.add_argument("--out", required=True, type=Path, help="folder for the manifest")
    parser.add_argument("--key-stdin", action="store_true", help="read the private key from stdin")
    parser.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="without a key, write an unsigned manifest (never published) instead of failing",
    )
    parser.add_argument("--lib", type=Path, help="folder that holds cryptography (and only it)")
    parser.add_argument("--trust-list", type=Path, default=DEFAULT_TRUST_LIST)
    parser.add_argument("--node-protocol", help="supported node protocol range, e.g. 1-2")
    parser.add_argument("--min-node-version", help="oldest node version this core works with")
    parser.add_argument("files", nargs="+", type=Path, help="the files the release publishes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    key_text = sys.stdin.read() if args.key_stdin else ""
    if args.lib is not None:
        sys.path.insert(0, str(args.lib.resolve()))
    try:
        protocol = None
        if args.node_protocol:
            match = _PROTOCOL.match(args.node_protocol)
            if match is None:
                raise RefusedError("--node-protocol must look like 1-2")
            protocol = (int(match.group(1)), int(match.group(2)))
        trusted = read_trust_list(args.trust_list)
        manifest = build_manifest(
            tag=args.tag,
            commit=args.commit,
            files=args.files,
            next_keys=trusted,
            node_protocol=protocol,
            min_node_version=args.min_node_version,
        )
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / SIGNATURE_NAME).unlink(missing_ok=True)
        if not key_text.strip():
            if not args.allow_unsigned:
                raise RefusedError("no signing key on stdin (--key-stdin)")
            (args.out / MANIFEST_NAME).write_bytes(manifest)
            _annotate(
                "warning",
                "Release not signed",
                f"The release key is not configured, so {args.tag} gets no signed manifest and "
                "installations that trust a release key will refuse it. The owner sets the key "
                "up as described in docs/RELEASES.md.",
            )
            _github_output("signed", "false")
            return 0
        signature = sign(manifest, key_text, trusted, args.tag)
    except RefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:  # unreadable files, bad base64 in the trust list
        print(f"refused: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1
    finally:
        key_text = ""
    (args.out / MANIFEST_NAME).write_bytes(manifest)
    (args.out / SIGNATURE_NAME).write_bytes(signature)
    key_id = json.loads(signature)["key_id"]
    print(f"Signed {args.tag} with release key {key_id}: {len(args.files)} files.")
    _github_output("signed", "true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
