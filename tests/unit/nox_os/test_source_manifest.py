"""os/tools/source_manifest.py: every package in the image is listed with its source and licence."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.nox_os.conftest import load_tool

manifest = load_tool("source_manifest")

STATUS = """\
Package: bash
Status: install ok installed
Architecture: arm64
Version: 5.2.37-2
Description: GNU Bourne Again SHell
 a continuation line

Package: libc6
Status: install ok installed
Architecture: arm64
Source: glibc (2.41-12)
Version: 2.41-12+rpt1

Package: gone
Status: deinstall ok config-files
Architecture: arm64
Version: 1.0
"""

COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/

Files: *
License: GPL-3+

Files: lib/*
License: GPL-3+

Files: doc/*
License: GFDL-1.3
"""


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "var/lib/dpkg").mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text(STATUS, "utf-8")
    (root / "usr/share/doc/bash").mkdir(parents=True)
    (root / "usr/share/doc/bash/copyright").write_text(COPYRIGHT, "utf-8")
    site = root / "opt/nox/current/.venv/lib/python3.13/site-packages"
    for name, version, header in (
        ("httpx", "0.28.1", "License-Expression: BSD-3-Clause"),
        ("legacy", "1.2", "Classifier: License :: OSI Approved :: MIT License"),
        ("plain", "3.0", "License: Apache License 2.0\n  long text that is cut"),
    ):
        info = site / f"{name}-{version}.dist-info"
        info.mkdir(parents=True)
        info.joinpath("METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n{header}\n", "utf-8"
        )
    (root / "etc").mkdir()
    (root / "etc/nox-os-release").write_text(
        "NOX_OS_VERSION=v0.7.0\nNOX_OS_BUILD_DATE=2026-10-22\n"
    )
    return root


def test_debian_packages_with_source_coordinates_and_licences(tmp_path: Path) -> None:
    packages = {p["package"]: p for p in manifest.debian_packages(_root(tmp_path))}
    assert set(packages) == {"bash", "libc6"}  # removed packages are not in the image
    assert packages["bash"] == {
        "package": "bash",
        "version": "5.2.37-2",
        "architecture": "arm64",
        "source": "bash",
        "source_version": "5.2.37-2",
        "licenses": ["GPL-3+", "GFDL-1.3"],
    }
    assert packages["libc6"]["source"] == "glibc"
    assert packages["libc6"]["source_version"] == "2.41-12"
    assert packages["libc6"]["licenses"] == []  # no copyright file: nothing invented


def test_python_packages_in_noxs_environment(tmp_path: Path) -> None:
    licences = {p["name"]: p["license"] for p in manifest.python_packages(_root(tmp_path))}
    assert licences == {
        "httpx": "BSD-3-Clause",
        "legacy": "MIT License",
        "plain": "Apache License 2.0",
    }


def test_the_document(tmp_path: Path) -> None:
    out = tmp_path / "nox-os-v0.7.0-arm64.licenses.json"
    root = _root(tmp_path)
    assert (
        manifest.main(["--root", str(root), "--image", "nox-os-v0.7.0-arm64", "--out", str(out)])
        == 0
    )
    document = json.loads(out.read_text("utf-8"))
    assert document["schema"] == "nox-os-sources/1"
    assert document["name"] == "Nox-OS (based on Raspberry Pi OS)"
    assert document["release"]["NOX_OS_VERSION"] == "v0.7.0"
    assert any("Claude Code" in item for item in document["not_bundled"])
    assert len(document["debian_packages"]) == 2 and len(document["python_packages"]) == 3


def test_a_linked_copyright_file_is_not_followed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "outside"
    target.write_text("License: Secret-1.0\n", "utf-8")
    link = root / "usr/share/doc/libc6/copyright"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:  # Windows without the symlink privilege
        return
    packages = {p["package"]: p for p in manifest.debian_packages(root)}
    assert packages["libc6"]["licenses"] == []


def test_the_absolute_current_link_means_the_images_release(tmp_path: Path) -> None:
    # /opt/nox/current -> /opt/nox/releases/<commit> is absolute: inside the mounted image, not on
    # the machine that mounted it.
    root = _root(tmp_path)
    nox = root / "opt/nox"
    (nox / "releases").mkdir()
    (nox / "current").rename(nox / "releases/0123456789ab")
    try:
        (nox / "current").symlink_to("/opt/nox/releases/0123456789ab")
    except OSError:  # Windows without the symlink privilege
        return
    names = {p["name"] for p in manifest.python_packages(root)}
    assert names == {"httpx", "legacy", "plain"}
