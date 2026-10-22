"""os/tools/imager_json.py: the Raspberry Pi Imager entry describes exactly the published image."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from tests.unit.nox_os.conftest import load_tool

imager = load_tool("imager_json")


def _files(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "nox-os-v0.7.0-arm64.img"
    raw.write_bytes(b"\0" * 3000 + b"nox")
    packed = tmp_path / "nox-os-v0.7.0-arm64.img.xz"
    packed.write_bytes(b"packed")
    return raw, packed


def test_the_entry_carries_both_sizes_and_checksums(tmp_path: Path) -> None:
    raw, packed = _files(tmp_path)
    out = tmp_path / "nox-os.rpi-imager.json"
    url = "https://github.com/Crackxsy/nox/releases/download/v0.7.0/nox-os-v0.7.0-arm64.img.xz"
    assert (
        imager.main(
            [
                "--raw",
                str(raw),
                "--image",
                str(packed),
                "--url",
                url,
                "--version",
                "v0.7.0",
                "--release-date",
                "2026-10-22",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    (item,) = json.loads(out.read_text("utf-8"))["os_list"]
    assert item["url"] == url
    assert item["extract_size"] == raw.stat().st_size
    assert item["extract_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert item["image_download_size"] == len(b"packed")
    assert item["image_download_sha256"] == hashlib.sha256(b"packed").hexdigest()
    assert item["release_date"] == "2026-10-22"
    assert item["name"] == "Nox-OS (based on Raspberry Pi OS)"
    assert set(item["devices"]) == {"pi5-64bit", "pi4-64bit", "pi3-64bit"}
    assert "/v0.7.0/" in item["icon"]


def test_imager_customisation_is_not_offered(tmp_path: Path) -> None:
    # No init_format: Imager's user/password/SSH-key customisation would bypass Nox-OS's setup.
    raw, packed = _files(tmp_path)
    item = imager.entry(
        raw=imager.digest(raw),
        download=imager.digest(packed),
        url="nox-os-x.img.xz",
        version="0123456789ab",
        release_date=dt.date(2026, 10, 22),
    )
    assert "init_format" not in item


@pytest.mark.parametrize(
    "url", ["http://example.invalid/nox.img.xz", "file:///tmp/nox.img.xz", "../nox.img.xz", ""]
)
def test_only_https_or_a_bare_file_name(tmp_path: Path, url: str) -> None:
    raw, packed = _files(tmp_path)
    with pytest.raises(ValueError):
        imager.entry(
            raw=imager.digest(raw),
            download=imager.digest(packed),
            url=url,
            version="v0.7.0",
            release_date=dt.date(2026, 10, 22),
        )


def test_a_version_cannot_inject_into_the_icon_url(tmp_path: Path) -> None:
    raw, packed = _files(tmp_path)
    with pytest.raises(ValueError):
        imager.entry(
            raw=imager.digest(raw),
            download=imager.digest(packed),
            url="nox.img.xz",
            version="v0.7.0/../../evil",
            release_date=dt.date(2026, 10, 22),
        )
