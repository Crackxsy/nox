"""`installer/build.py`: pinned downloads, hash-checked dependencies, the licenses it ships.

Nothing here downloads anything: the network is replaced by a function that writes bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


def _build() -> ModuleType:
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("nox_installer_build_pipeline", str(REPO / "installer" / "build.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_every_download_is_pinned_to_an_immutable_url_and_a_sha256() -> None:
    build = _build()
    zip_name = f"python-{build.DEFAULT_PYTHON_VERSION}-embed-amd64.zip"
    assert set(build.PINNED_SHA256) == {zip_name, "get-pip.py"}
    for digest in build.PINNED_SHA256.values():
        assert len(digest) == 64 and int(digest, 16) >= 0
    # A commit of pypa/get-pip, not the moving bootstrap.pypa.io URL.
    assert build.GET_PIP_URL.startswith("https://raw.githubusercontent.com/pypa/get-pip/")
    assert len(build.GET_PIP_URL.split("/")[5]) == 40
    assert "https://bootstrap.pypa.io" not in (REPO / "installer" / "build.py").read_text("utf-8")


def _fake_fetch(payload: bytes) -> Any:
    def fetch(url: str, dest: Path) -> None:
        Path(dest).write_bytes(payload)

    return fetch


def test_a_download_with_the_wrong_hash_is_refused_and_not_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _build()
    good = b"the real get-pip"
    monkeypatch.setitem(build.PINNED_SHA256, "get-pip.py", hashlib.sha256(good).hexdigest())
    monkeypatch.setattr(build.urllib.request, "urlretrieve", _fake_fetch(b"something else"))
    dest = tmp_path / "get-pip.py"
    with pytest.raises(RuntimeError, match="SHA-256"):
        build.download("https://example.invalid/get-pip.py", dest)
    assert not dest.exists() and not list(tmp_path.iterdir())

    monkeypatch.setattr(build.urllib.request, "urlretrieve", _fake_fetch(good))
    assert build.download("https://example.invalid/get-pip.py", dest).read_bytes() == good


def test_a_tampered_cached_file_is_downloaded_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _build()
    good = b"pinned bytes"
    monkeypatch.setitem(build.PINNED_SHA256, "get-pip.py", hashlib.sha256(good).hexdigest())
    dest = tmp_path / "get-pip.py"
    dest.write_bytes(b"tampered cache")
    monkeypatch.setattr(build.urllib.request, "urlretrieve", _fake_fetch(good))
    assert build.download("https://example.invalid/get-pip.py", dest).read_bytes() == good


def test_an_unpinned_file_cannot_be_downloaded(tmp_path: Path) -> None:
    build = _build()
    with pytest.raises(RuntimeError, match="no pinned SHA-256"):
        build.download("https://example.invalid/x.zip", tmp_path / "python-9.9.9-embed-amd64.zip")


def test_the_lock_is_installed_with_require_hashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _build()
    lock = (REPO / "installer" / "requirements.lock").read_text(encoding="utf-8")
    entries = build.filtered_requirements(lock)
    assert entries and all("--hash=sha256:" in e for e in entries)
    assert not any(e.startswith("-e ") or e.startswith("#") for e in entries)
    assert any(e.startswith("piper-tts==") for e in entries)

    calls: list[list[str]] = []
    monkeypatch.setattr(build, "INSTALLER_DIR", tmp_path)
    monkeypatch.setattr(build.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    build.step_install_dependencies(tmp_path, REPO / "installer" / "requirements.lock")
    assert "--require-hashes" in calls[0]

    unhashed = tmp_path / "plain.lock"
    unhashed.write_text("-e .\nrequests==2.0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="without hashes"):
        build.step_install_dependencies(tmp_path, unhashed)


def test_continuation_lines_are_joined_and_comments_dropped() -> None:
    build = _build()
    text = (
        "# header\n-e .\n"
        "a==1 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n    # via b\n"
        "c==2 ; sys_platform == 'win32' \\\n    --hash=sha256:cc\n"
    )
    assert build.filtered_requirements(text) == [
        "a==1 --hash=sha256:aa --hash=sha256:bb",
        "c==2 ; sys_platform == 'win32' --hash=sha256:cc",
    ]


def test_the_export_keeps_hashes_and_never_touches_uv_lock() -> None:
    source = (REPO / "installer" / "build.py").read_text(encoding="utf-8")
    assert "--no-hashes" not in source and '"--frozen"' in source


def test_the_build_ships_the_licenses_and_a_written_source_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _build()
    monkeypatch.setattr(build, "LEGAL_DIR", tmp_path / "legal")
    monkeypatch.setattr(build, "_git", lambda *a: "feedface" if "rev-parse" in a else "")
    build.step_legal("9.9.9")
    shipped = {p.name for p in (tmp_path / "legal").iterdir()}
    assert shipped == {
        "LICENSE",
        "NOTICE",
        "COPYING",
        "THIRD_PARTY_LICENSES.md",
        "SOURCE.txt",
        "INSTALLER_LICENSE.txt",
    }
    copying = (tmp_path / "legal" / "COPYING").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in copying and "Version 3, 29 June 2007" in copying
    offer = (tmp_path / "legal" / "SOURCE.txt").read_text(encoding="ascii")
    assert "https://github.com/Crackxsy/nox/tree/feedface" in offer
    assert "three years" in offer and "python installer/build.py" in offer
    wizard = (tmp_path / "legal" / "INSTALLER_LICENSE.txt").read_text(encoding="ascii")
    assert "GPL-3.0-or-later" in wizard and offer.splitlines()[0] in wizard


def test_the_repository_carries_the_gpl_text() -> None:
    data = (REPO / "COPYING").read_bytes()
    # The canonical https://www.gnu.org/licenses/gpl-3.0.txt.
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == (
        "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
    )


def test_the_build_writes_the_pinned_sandbox_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from nox.sandbox import wfp_helper

    build = _build()
    monkeypatch.setattr(build, "SANDBOX_RULES_DIR", tmp_path / "rules")
    build.step_sandbox_rules()
    script = tmp_path / "rules" / "nox-sandbox-rules.ps1"
    include = (tmp_path / "rules" / "sandbox_rules.iss").read_text(encoding="ascii")
    digest = hashlib.sha256(script.read_bytes()).hexdigest().upper()
    assert digest == wfp_helper.helper_sha256()
    assert f"Result := '{digest}';" in include
