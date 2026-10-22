"""`nox release ...`: check a release manifest and show the release keys this version trusts.

Exit status, for scripts (`nox update` on Linux uses it): 0 verified, 1 refused, 2 usage error,
3 release signing is not configured in this version (no trusted key) - which is reported as such
and never as "verified".
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import typer

__all__ = ["EXIT_NOT_CONFIGURED", "EXIT_REFUSED", "release_app"]

EXIT_REFUSED = 1
EXIT_NOT_CONFIGURED = 3

release_app = typer.Typer(
    help="Signed releases: verify a release manifest, list the trusted release keys.",
    no_args_is_help=True,
)


def _read_limited(path: Path, limit: int, what: str) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        typer.echo(f"Refused: cannot read the {what} {path}: {exc.strerror or exc}", err=True)
        raise typer.Exit(EXIT_REFUSED) from None
    if len(data) > limit:
        typer.echo(f"Refused: the {what} {path} is far too large.", err=True)
        raise typer.Exit(EXIT_REFUSED)
    return data


@release_app.command("verify")
def verify(
    manifest: Annotated[Path, typer.Argument(help="The release-manifest.json to check.")],
    signature: Annotated[
        Path | None,
        typer.Option(help="Its detached signature. Default: the manifest's path plus .sig."),
    ] = None,
    file: Annotated[
        list[Path] | None,
        typer.Option(
            "--file", help="A downloaded release file to compare with the manifest (repeatable)."
        ),
    ] = None,
    expect_tag: Annotated[
        str | None, typer.Option(help="Refuse unless the manifest is for this tag.")
    ] = None,
    expect_commit: Annotated[
        str | None, typer.Option(help="Refuse unless the manifest names this source commit.")
    ] = None,
    min_version: Annotated[
        str | None, typer.Option(help="Refuse versions below this one (rollback protection).")
    ] = None,
    allow_downgrade: Annotated[
        bool,
        typer.Option(
            "--allow-downgrade", help="Accept a version below --min-version (deliberate)."
        ),
    ] = False,
    kind: Annotated[
        str,
        typer.Option(
            help="Which manifest this is: release (release-manifest.json) or os-image "
            "(nox-os-manifest.json, the Nox-OS image files)."
        ),
    ] = "release",
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Verify a release manifest's signature and contents. Fails closed."""
    from nox.release import trusted_release_keys  # noqa: PLC0415 - keeps `nox --help` instant
    from nox.release.manifest import MAX_MANIFEST_BYTES  # noqa: PLC0415
    from nox.release.verify import (  # noqa: PLC0415
        MAX_SIGNATURE_BYTES,
        ReleaseVerificationError,
        SigningNotConfiguredError,
        verify_file,
        verify_release,
    )

    def refuse(error: ReleaseVerificationError) -> typer.Exit:
        code = EXIT_NOT_CONFIGURED if isinstance(error, SigningNotConfiguredError) else EXIT_REFUSED
        if as_json:
            typer.echo(json.dumps({"verified": False, "code": error.code, "reason": str(error)}))
        else:
            label = "Not verified" if code == EXIT_NOT_CONFIGURED else "Refused"
            typer.echo(f"{label}: {error}", err=True)
        return typer.Exit(code)

    files = file or []
    keys = trusted_release_keys()
    if not keys:
        raise refuse(SigningNotConfiguredError())
    manifest_bytes = _read_limited(manifest, MAX_MANIFEST_BYTES, "manifest")
    signature_path = signature or manifest.with_name(manifest.name + ".sig")
    signature_bytes = _read_limited(signature_path, MAX_SIGNATURE_BYTES, "signature")
    try:
        release = verify_release(
            manifest_bytes,
            signature_bytes,
            trusted_keys=keys,
            expect_tag=expect_tag,
            expect_commit=expect_commit,
            min_version=min_version,
            allow_downgrade=allow_downgrade,
            expect_kind=kind,
        )
        for path in files:
            if not path.is_file():
                raise ReleaseVerificationError("missing", f"{path} is not a file")
            verify_file(release, path)
    except ReleaseVerificationError as error:
        raise refuse(error) from None

    found = release.manifest
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "verified": True,
                    "key_id": release.key_id,
                    "version": str(found.version),
                    "tag": found.tag,
                    "channel": found.channel,
                    "source_commit": found.source_commit,
                    "files_checked": [path.name for path in files],
                    "assets": [
                        {"name": a.name, "size": a.size, "sha256": a.sha256} for a in found.assets
                    ],
                }
            )
        )
        return
    typer.echo(f"Verified: Nox {found.tag} ({found.channel}), signed by key {release.key_id}.")
    typer.echo(f"Source commit: {found.source_commit}")
    for path in files:
        typer.echo(f"  ok  {path.name}")


@release_app.command("keys")
def keys(
    as_json: Annotated[bool, typer.Option("--json", help="Print the list as JSON.")] = False,
) -> None:
    """List the release keys this version of Nox trusts, and whether each is still valid."""
    from nox.release import trusted_release_keys  # noqa: PLC0415

    today = dt.datetime.now(dt.UTC).date()
    entries = trusted_release_keys()
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "key_id": key.key_id,
                        "public_key": key.public_key,
                        "not_after": key.not_after,
                        "valid": key.valid_on(today),
                    }
                    for key in entries
                ]
            )
        )
    elif not entries:
        typer.echo("No release key is trusted yet: release signing is not configured.")
        typer.echo("See docs/RELEASES.md for how the owner sets it up.")
    else:
        for key in entries:
            state = "valid" if key.valid_on(today) else "EXPIRED"
            typer.echo(f"{key.key_id}  until {key.not_after}  {state}")
    if not entries:
        raise typer.Exit(EXIT_NOT_CONFIGURED)


@release_app.command("describe-key")
def describe_key(
    public_key: Annotated[Path, typer.Argument(help="A public key file (`openssl pkey -pubout`).")],
    not_after: Annotated[str, typer.Option(help="Last valid day, YYYY-MM-DD (UTC).")],
) -> None:
    """Print the trust-list entries for a public key - for the owner adding a release key.

    Only ever give this the PUBLIC key; it refuses anything else.
    """
    from nox.release.keys import TrustedKey, describe_public_key, parse_public_key  # noqa: PLC0415

    text = _read_limited(public_key, 4096, "public key").decode("ascii", errors="replace")
    if "PRIVATE" in text:
        typer.echo("Refused: that is a private key. Give the public key (openssl pkey -pubout).")
        raise typer.Exit(EXIT_REFUSED)
    try:
        key_id, spki = describe_public_key(parse_public_key(text))
        TrustedKey(key_id=key_id, public_key=spki, not_after=not_after)
    except ValueError as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(EXIT_REFUSED) from None
    typer.echo("src/nox/release/trusted_keys.py, inside TRUSTED_KEYS = ( ... ):")
    typer.echo(f'    TrustedKey(key_id="{key_id}", public_key="{spki}", not_after="{not_after}"),')
    typer.echo("")
    typer.echo("scripts/install-linux.sh and scripts/install-macos.sh,")
    typer.echo("inside TRUSTED_RELEASE_KEYS=( ... ):")
    typer.echo(f'  "{key_id}|{not_after}|{spki}"')
