"""The sandbox-rules helper against the real Base Filtering Engine (install, status, uninstall).

Runs only where it may change the machine: Windows, an administrator, and `NOX_WFP_LIVE=1` (set in
CI - the hosted Windows runners run as administrators and are thrown away afterwards). It starts
the helper exactly as the installer does: the system's PowerShell with the hash-checking bootstrap.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nox.sandbox import wfp, wfp_helper

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("NOX_WFP_LIVE") != "1" or not wfp.is_elevated(),
    reason="changes the machine's WFP state: Windows, administrator and NOX_WFP_LIVE=1 only",
)

POWERSHELL = Path(os.environ.get("SystemRoot", "C:\\Windows")) / (
    "System32\\WindowsPowerShell\\v1.0\\powershell.exe"
)


def _run(action: str, paths: list[Path]) -> subprocess.CompletedProcess[str]:
    command = f'"{POWERSHELL}" ' + wfp_helper.command_params(action, [str(p) for p in paths])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _status() -> dict[str, object] | None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, wfp_helper.STATUS_KEY)
    except FileNotFoundError:
        return None
    with key:
        return {
            name: winreg.QueryValueEx(key, name)[0]
            for name in ("Sequence", "Action", "Code", "Message", "Filters")
        }


def _protected() -> Path:
    buf = ctypes.create_unicode_buffer(260)
    # CSIDL_PROGRAM_FILES (0x26): what the helper's GetFolderPath('ProgramFiles') returns.
    assert ctypes.windll.shell32.SHGetFolderPathW(None, 0x26, None, 0, buf) == 0  # type: ignore[attr-defined]
    return Path(buf.value) / wfp_helper.HELPER_DIR / wfp_helper.HELPER_FILE


def test_install_status_and_uninstall_against_the_real_engine(tmp_path: Path) -> None:
    app_copy = tmp_path / "sandbox" / wfp_helper.HELPER_FILE
    app_copy.parent.mkdir()
    app_copy.write_bytes(wfp_helper.helper_bytes())

    # A tampered copy is refused before a byte of it runs.
    tampered = tmp_path / "tampered.ps1"
    tampered.write_bytes(wfp_helper.helper_bytes() + b"\n# changed\n")
    assert _run("install", [tampered]).returncode == 3

    result = _run("install", [app_copy])
    assert result.returncode == 0, result.stderr
    status = _status()
    assert status is not None and status["Code"] == 0, status
    assert status["Filters"] == len(wfp.all_rules())
    protected = _protected()
    assert protected.read_bytes() == wfp_helper.helper_bytes()

    assert _run("status", [app_copy]).returncode == 0  # Nox's provider is there
    # Installing again replaces every filter instead of duplicating it.
    assert _run("install", [app_copy]).returncode == 0

    result = _run("uninstall", [protected, app_copy])
    assert result.returncode == 0, result.stderr
    assert _run("status", [app_copy]).returncode == 1  # provider gone
    assert _status() is None
    assert not protected.parent.exists()
