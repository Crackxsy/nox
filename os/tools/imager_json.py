"""Write the Raspberry Pi Imager entry for a Nox-OS image (os/README.md).

    python os/tools/imager_json.py --raw nox-os-v0.7.0-arm64.img \\
        --image nox-os-v0.7.0-arm64.img.xz \\
        --url https://github.com/Crackxsy/nox/releases/download/v0.7.0/nox-os-v0.7.0-arm64.img.xz \\
        --version v0.7.0 --out nox-os.rpi-imager.json

The result is a custom repository for Raspberry Pi Imager ("App options" -> "Content repository"):
one `os_list` entry with the download's size and SHA-256 and the unpacked image's size and SHA-256,
which Imager checks after writing the card. It has no `init_format` on purpose: Imager's own
customisation (user, password, SSH key, Wi-Fi) would bypass Nox-OS's setup, so Imager does not
offer it for this image. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

NAME = "Nox-OS (based on Raspberry Pi OS)"
DESCRIPTION = (
    "Nox, the local-first AI companion, as a headless home server: Raspberry Pi OS Lite (64-bit) "
    "with Nox preinstalled. Set up from your phone; no screen, keyboard or SSH needed."
)
WEBSITE = "https://github.com/Crackxsy/nox"
ICON = "https://raw.githubusercontent.com/Crackxsy/nox/{ref}/ui/dashboard/public/icon-192.png"
#: Imager's device tags for every 64-bit Pi the base supports (3B/3B+/Zero 2 W, 4B/400/CM4, 5/CM5).
DEVICES = ("pi5-64bit", "pi4-64bit", "pi3-64bit")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


def digest(path: Path) -> tuple[int, str]:
    """Size and SHA-256 of a file, read in chunks (images are gigabytes)."""
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            sha.update(chunk)
            size += len(chunk)
    return size, sha.hexdigest()


def entry(
    *,
    raw: tuple[int, str],
    download: tuple[int, str],
    url: str,
    version: str,
    release_date: dt.date,
) -> dict[str, object]:
    """One `os_list` entry. `url` is an https URL, or a bare file name for a local test build."""
    if not _REF.match(version):
        raise ValueError(f"invalid version: {version!r}")
    if not (url.startswith("https://") or _FILE_NAME.match(url)):
        raise ValueError("url must be an https URL or a bare file name")
    return {
        "name": NAME,
        "description": DESCRIPTION,
        "icon": ICON.format(ref=version),
        "website": WEBSITE,
        "url": url,
        "release_date": release_date.isoformat(),
        "extract_size": raw[0],
        "extract_sha256": raw[1],
        "image_download_size": download[0],
        "image_download_sha256": download[1],
        "devices": list(DEVICES),
        "capabilities": [],
    }


def document(item: dict[str, object]) -> str:
    return json.dumps({"os_list": [item]}, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True, help="the unpacked .img")
    parser.add_argument("--image", type=Path, required=True, help="the download (.img.xz)")
    parser.add_argument("--url", required=True, help="where the download is published")
    parser.add_argument("--version", required=True, help="the Nox version (tag or short commit)")
    parser.add_argument("--release-date", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    item = entry(
        raw=digest(args.raw),
        download=digest(args.image),
        url=args.url,
        version=args.version,
        release_date=args.release_date or dt.datetime.now(dt.UTC).date(),
    )
    args.out.write_text(document(item), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
