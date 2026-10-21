"""Rewrite scripts/signing-requirements.txt from uv.lock (after `cryptography` was updated there).

    python scripts/update_signing_requirements.py

The release signer imports only `cryptography`; its requirements carry the versions and SHA-256
hashes uv.lock already pins, so the job that holds the release key installs nothing that was not
reviewed with the lock file. tests/unit/release/test_sign_script.py fails while the two differ.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "scripts" / "signing-requirements.txt"
PACKAGES = ("cryptography", "cffi", "pycparser")
HEADER = """\
# The only packages the release signer (scripts/sign_release.py) may import: cryptography and
# what it needs. Versions and SHA-256 hashes are copied from uv.lock (all published wheels, so it
# installs on any platform); tests/unit/release/test_sign_script.py fails when they drift.
# Regenerate: python scripts/update_signing_requirements.py
# Install: pip install --require-hashes --no-deps --only-binary :all: --target <dir> -r <this>
"""


def render(lock_text: str) -> str:
    lock = tomllib.loads(lock_text)
    lines = [HEADER.rstrip("\n")]
    for name in PACKAGES:
        package = next(p for p in lock["package"] if p["name"] == name)
        hashes = sorted({wheel["hash"] for wheel in package["wheels"]})
        lines.append(f"{name}=={package['version']} \\")
        lines.extend(
            f"    --hash={digest}" + (" \\" if index < len(hashes) - 1 else "")
            for index, digest in enumerate(hashes)
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    TARGET.write_text(render((REPO / "uv.lock").read_text("utf-8")), "utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
