"""End-to-end check of the built Windows installer (CI: `.github/workflows/installer-windows.yml`).

Changes the machine: it installs Nox for the current user, adds the sandbox network rules (the
hosted runner is an administrator, so the installer's one elevated step runs without a prompt),
starts `nox doctor` from the installed copy, installs over itself, uninstalls, and checks that the
program files, the rules, their status key and the protected helper folder are gone. Run it only
on a throw-away machine:

    build\runtime\python.exe installer/ci_smoke.py installer/out/Nox-Setup-<version>.exe --logs <dir>

(the built runtime: it has every dependency of the `nox` package this script imports).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nox.sandbox import wfp_helper  # noqa: E402

APP = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Nox"
PROTECTED = Path(wfp_helper.program_files_dir()) / wfp_helper.HELPER_DIR
#: The helper as built - the same bytes (and pinned hash) the installer carries.
BUILT_HELPER = REPO_ROOT / "build" / "sandbox-rules" / wfp_helper.HELPER_FILE
POWERSHELL = Path(wfp_helper.system_dir()) / wfp_helper.POWERSHELL_IN_SYSTEM32
QUIET = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


def step(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)  # noqa: T201 - a CI script


def check(ok: bool, msg: str) -> None:
    if not ok:
        raise SystemExit(f"[smoke] FAILED: {msg}")
    step(f"ok: {msg}")


def status_key() -> dict[str, object] | None:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            wfp_helper.STATUS_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return None
    with key:
        values: dict[str, object] = {}
        for name in ("Sequence", "Code", "Message", "Filters"):
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[name] = None
        return values


def rules_present() -> bool:
    """The helper's own `status`, not elevated: exit 0 = Nox's provider is there, 1 = not."""
    command = f'"{POWERSHELL}" ' + wfp_helper.command_params("status", [str(BUILT_HELPER)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode not in (0, 1):
        raise SystemExit(f"[smoke] helper status failed: {result.returncode} {result.stderr}")
    return result.returncode == 0


def run_setup(setup: Path, log: Path) -> None:
    cmd = [str(setup), *QUIET, "/TASKS=sandboxrules", f"/LOG={log}"]
    step(f"running {' '.join(cmd)}")
    result = subprocess.run(cmd, timeout=1800, check=False)
    check(result.returncode == 0, f"setup exited with {result.returncode}")


def check_installed() -> int:
    for rel in (
        "runtime/python.exe",
        "runtime/pythonw.exe",
        "app/nox/__init__.py",
        "app/start_nox.pyw",
        "sandbox/nox-sandbox-rules.ps1",
        "LICENSE",
        "COPYING",
        "SOURCE.txt",
        "unins000.exe",
    ):
        check((APP / rel).is_file(), f"installed {rel}")
    status = status_key()
    check(status is not None and status["Code"] == 0, f"rule step reported success: {status}")
    check((PROTECTED / wfp_helper.HELPER_FILE).is_file(), f"protected copy in {PROTECTED}")
    check(rules_present(), "Nox's WFP provider and filters are in place")
    doctor = subprocess.run(
        [str(APP / "runtime" / "python.exe"), "-m", "nox.cli", "doctor"],
        cwd=APP / "app",
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    print(doctor.stdout, doctor.stderr, sep="\n", flush=True)  # noqa: T201
    check(doctor.returncode == 0 and "[ ok ]" in doctor.stdout, "nox doctor from the install")
    assert status is not None
    sequence = status["Sequence"]
    return sequence if isinstance(sequence, int) else 0


def uninstall(log: Path) -> None:
    uninstaller = APP / "unins000.exe"
    cmd = [str(uninstaller), *QUIET, f"/LOG={log}"]
    step(f"running {' '.join(cmd)}")
    subprocess.run(cmd, timeout=600, check=False)
    # The uninstaller restarts itself from a temporary copy and returns at once: wait for it.
    deadline = time.monotonic() + 600
    while uninstaller.exists() and time.monotonic() < deadline:
        time.sleep(2)
    check(not uninstaller.exists(), "the uninstaller finished")


def check_removed() -> None:
    for rel in ("runtime", "app", "sandbox"):
        check(not (APP / rel).exists(), f"removed {rel}")
    check(status_key() is None, "status key removed")
    check(not PROTECTED.exists(), "protected helper folder removed")
    check(not rules_present(), "no filter of Nox's provider left")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("setup", type=Path)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    check(wfp_helper.helper_sha256() is not None and BUILT_HELPER.is_file(), "built helper")
    check(not APP.exists() and status_key() is None, "a clean machine to start from")

    step("1/3 fresh install")
    run_setup(args.setup, args.logs / "install.log")
    first = check_installed()

    step("2/3 install over itself (upgrade path)")
    run_setup(args.setup, args.logs / "upgrade.log")
    second = check_installed()
    check(second > first, f"the rule step ran again (sequence {first} -> {second})")

    step("3/3 uninstall")
    uninstall(args.logs / "uninstall.log")
    check_removed()
    step("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
