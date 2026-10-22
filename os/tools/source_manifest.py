"""List what a Nox-OS image contains and under which licences (spec 2.10; os/README.md).

    python os/tools/source_manifest.py --root <mounted root file system> \\
        --image nox-os-v0.7.0-arm64 \\
        --out nox-os-v0.7.0-arm64.licenses.json

A first, generated manifest: every Debian / Raspberry Pi OS package with its version and source
package (the coordinates of its corresponding source code in the Debian and Raspberry Pi archives),
the licence names its copyright file declares, and every Python package in Nox's environment with
its declared licence. Mirroring the GPL source packages as a release asset is a later step (O8);
until then the manifest says where the sources are. Reads files only; standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from email.parser import HeaderParser
from pathlib import Path

SCHEMA = "nox-os-sources/1"
NAME = "Nox-OS (based on Raspberry Pi OS)"
ARCHIVES = ("https://deb.debian.org/debian", "https://archive.raspberrypi.com/debian")
#: Components that are never part of the image (spec 2.10): the user installs them by their own
#: action, or Nox downloads them on demand after asking.
NOT_BUNDLED = (
    "Claude Code (proprietary; installed by the user's action via the official installer)",
    "Ollama and local models",
    "Playwright Chromium",
    "Voice models (Supertonic, Piper voices) and Whisper weights",
)
_LICENSE_LINE = re.compile(r"^License:\s*(\S.*?)\s*$", re.M)
_MAX_COPYRIGHT = 2 * 1024 * 1024


def _paragraphs(text: str) -> list[dict[str, str]]:
    """The stanzas of a dpkg status file as dicts (continuation lines folded in)."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key = ""
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
            current, key = {}, ""
        elif line[0] in " \t":
            if key:
                current[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def _copyright_licenses(root: Path, package: str) -> list[str]:
    """Licence short names a DEP-5 copyright file declares (none when not machine-readable)."""
    path = root / "usr" / "share" / "doc" / package / "copyright"
    try:
        # A link may point anywhere on the machine that mounted the image: only plain files.
        if any(p.is_symlink() for p in (path, path.parent)) or not path.is_file():
            return []
        if path.stat().st_size > _MAX_COPYRIGHT:
            return []
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for match in _LICENSE_LINE.finditer(text):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def debian_packages(root: Path) -> list[dict[str, object]]:
    status = (root / "var" / "lib" / "dpkg" / "status").read_text("utf-8", errors="replace")
    packages: list[dict[str, object]] = []
    for stanza in _paragraphs(status):
        if stanza.get("Status", "").split()[-1:] != ["installed"] or "Package" not in stanza:
            continue
        name, version = stanza["Package"], stanza.get("Version", "")
        source, _, source_version = stanza.get("Source", name).partition(" ")
        packages.append(
            {
                "package": name,
                "version": version,
                "architecture": stanza.get("Architecture", ""),
                "source": source,
                "source_version": source_version.strip("()") or version,
                "licenses": _copyright_licenses(root, name),
            }
        )
    return sorted(packages, key=lambda item: str(item["package"]))


def _in_root(root: Path, path: Path) -> Path:
    """`path` of the image, following its links inside `root` - an absolute link such as
    /opt/nox/current -> /opt/nox/releases/<commit> means the image's file, not this machine's."""
    current = root / path.relative_to(path.anchor)
    for _ in range(8):
        if not current.is_symlink():
            return current
        target = Path(current.readlink())
        if target.is_absolute() or target.anchor:
            current = root / target.relative_to(target.anchor)
        else:
            current = current.parent / target
    return current


def python_packages(root: Path) -> list[dict[str, object]]:
    """The packages in Nox's environment (/opt/nox/current/.venv), from their metadata."""
    venv = _in_root(root, Path("/opt/nox/current")) / ".venv"
    found: dict[str, dict[str, object]] = {}
    for metadata in sorted(venv.glob("lib/python3*/site-packages/*.dist-info/METADATA")):
        try:
            headers = HeaderParser().parsestr(metadata.read_text("utf-8", errors="replace"))
        except OSError:
            continue
        name = headers.get("Name")
        if not name:
            continue
        license_ = headers.get("License-Expression") or ""
        if not license_:
            classifiers = [
                c for c in headers.get_all("Classifier") or [] if c.startswith("License ::")
            ]
            license_ = "; ".join(c.rsplit(" :: ", 1)[-1] for c in classifiers)
        if not license_:
            lines = (headers.get("License") or "").strip().splitlines()
            license_ = lines[0][:200] if lines else ""
        found[name.lower()] = {
            "name": name,
            "version": headers.get("Version", ""),
            "license": license_ or "UNKNOWN",
        }
    return [found[key] for key in sorted(found)]


def nox_release(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = (root / "etc" / "nox-os-release").read_text("utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.startswith("NOX_OS_"):
            values[key] = value.strip("'\"")
    return values


def build(
    root: Path,
    image: str,
    *,
    debian: list[dict[str, object]] | None = None,
    python: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "image": image,
        "name": NAME,
        "release": nox_release(root),
        "components": [
            {"name": "Nox", "license": "Apache-2.0", "source": "https://github.com/Crackxsy/nox"},
            {
                "name": "pi-gen (build tool, not shipped)",
                "license": "BSD-3-Clause",
                "source": "https://github.com/RPi-Distro/pi-gen",
            },
        ],
        "source_code": {
            "note": (
                "Every Debian / Raspberry Pi OS package below is listed with its source package "
                "and version: its corresponding source code is in the archives named here. "
                "Mirroring the GPL source packages as a release asset follows (Nox-OS step O8)."
            ),
            "archives": list(ARCHIVES),
        },
        "not_bundled": list(NOT_BUNDLED),
        "debian_packages": debian_packages(root) if debian is None else debian,
        "python_packages": python_packages(root) if python is None else python,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True, help="the image's root file system")
    parser.add_argument("--image", required=True, help="the image's name, e.g. nox-os-v0.7.0-arm64")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    debian, python = debian_packages(args.root), python_packages(args.root)
    manifest = build(args.root, args.image, debian=debian, python=python)
    args.out.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8", newline="\n")
    sys.stdout.write(f"{args.out}: {len(debian)} Debian packages, {len(python)} Python packages\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
