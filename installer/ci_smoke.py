"""End-to-end check of the built Windows installer (CI: `.github/workflows/installer-windows.yml`).

Changes the machine: it installs Nox for the current user, adds the sandbox network rules (the
hosted runner is an administrator, so the installer's one elevated step runs without a prompt),
starts `nox doctor` from the installed copy, installs over itself, uninstalls, and checks that the
program files, the rules, their status key and the protected helper folder are gone. Run it only
on a throw-away machine:

    build/runtime/python.exe installer/ci_smoke.py installer/out/Nox-Setup-<v>.exe --logs <dir>

(the built runtime: it has every dependency of the `nox` package this script imports).
"""

from __future__ import annotations

import argparse
import os
import shutil
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


#: One setup or uninstall run, at most: a real one takes about 90 s on a hosted runner. A hang
#: (a dialog nobody can answer) fails fast, with the tail of the setup log in the CI output.
STEP_TIMEOUT_S = 300


def kill_tree(pid: int) -> None:
    taskkill = Path(wfp_helper.system_dir()) / "taskkill.exe"
    subprocess.run([str(taskkill), "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)


def log_tail(log: Path, lines: int = 40) -> str:
    try:
        text = log.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return f"(no log at {log})"
    return "\n".join(text[-lines:])


def uac_on() -> bool:
    """User Account Control is on: an administrator's processes are elevated only on purpose."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return True
    with key:
        try:
            return int(winreg.QueryValueEx(key, "EnableLUA")[0]) != 0
        except FileNotFoundError:
            return True


def start_limited(cmd: list[str]) -> tuple[int, int]:
    """Start `cmd` as this account without administrator rights: the Safer "normal user" level
    (the Administrators group deny-only, as a standard user's token) at medium integrity - what
    a person gets who starts Setup normally while UAC is on. (process handle, pid)"""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def ok(result: int, what: str) -> None:
        if not result:
            raise OSError(ctypes.get_last_error(), what)

    level = wintypes.HANDLE()
    ok(advapi32.SaferCreateLevel(2, 0x20000, 1, ctypes.byref(level), None), "SaferCreateLevel")
    token = wintypes.HANDLE()
    ok(
        advapi32.SaferComputeTokenFromLevel(level, None, ctypes.byref(token), 0, None),
        "SaferComputeTokenFromLevel",
    )
    advapi32.SaferCloseLevel(level)

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    sid = ctypes.c_void_p()
    ok(advapi32.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid)), "medium integrity SID")
    label = SidAndAttributes(sid, 0x20)  # SE_GROUP_INTEGRITY
    size = ctypes.sizeof(label) + advapi32.GetLengthSid(sid)
    ok(
        advapi32.SetTokenInformation(token, 25, ctypes.byref(label), size),  # TokenIntegrityLevel
        "SetTokenInformation",
    )

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    startup = StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    info = ProcessInformation()
    line = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
    ok(
        advapi32.CreateProcessAsUserW(
            token,
            None,
            line,
            None,
            None,
            False,
            0x08000000,
            None,
            None,
            ctypes.byref(startup),
            ctypes.byref(info),
        ),
        "CreateProcessAsUserW",
    )
    kernel32.CloseHandle(info.hThread)
    kernel32.CloseHandle(token)
    return int(info.hProcess), int(info.dwProcessId)


def wait_limited(process: int, pid: int, log: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if kernel32.WaitForSingleObject(wintypes.HANDLE(process), STEP_TIMEOUT_S * 1000) != 0:
        kill_tree(pid)
        print(log_tail(log), flush=True)  # noqa: T201
        raise SystemExit(f"[smoke] FAILED: hung for {STEP_TIMEOUT_S} s (log tail above)")
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(wintypes.HANDLE(process), ctypes.byref(code))
    kernel32.CloseHandle(wintypes.HANDLE(process))
    return int(code.value)


def run_setup(setup: Path, log: Path, *extra: str, limited: bool = False) -> int:
    """Run setup silently; its exit code. `limited`: as this account without administrator
    rights. A hang is killed (with its children) after `STEP_TIMEOUT_S`."""
    cmd = [str(setup), *QUIET, *(extra or ("/TASKS=sandboxrules",)), f"/LOG={log}"]
    step(f"running {'(limited) ' if limited else ''}{' '.join(cmd)}")
    if limited:
        process, pid = start_limited(cmd)
        return wait_limited(process, pid, log)
    with subprocess.Popen(cmd) as running:
        try:
            return running.wait(timeout=STEP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_tree(running.pid)
            print(log_tail(log), flush=True)  # noqa: T201
            raise SystemExit(
                f"[smoke] FAILED: hung for {STEP_TIMEOUT_S} s (log tail above)"
            ) from None


def install(setup: Path, log: Path, *extra: str, limited: bool = False) -> None:
    code = run_setup(setup, log, *extra, limited=limited)
    if code != 0:
        print(log_tail(log), flush=True)  # noqa: T201
    check(code == 0, f"setup exited with {code}")


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
    if status is None:
        # A 32-bit helper would have reported here instead - say so, it is the likely cause.
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Nox\\SandboxRules")
            step("a status key exists in WOW6432Node: the helper ran as a 32-bit process")
        except FileNotFoundError:
            pass
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
    subprocess.run(cmd, timeout=STEP_TIMEOUT_S, check=False)
    # The uninstaller restarts itself from a temporary copy and returns at once: wait for it.
    deadline = time.monotonic() + STEP_TIMEOUT_S
    while uninstaller.exists() and time.monotonic() < deadline:
        time.sleep(2)
    if uninstaller.exists():
        print(log_tail(log), flush=True)  # noqa: T201
    check(not uninstaller.exists(), "the uninstaller finished")


def check_removed() -> None:
    for rel in ("runtime", "app", "sandbox"):
        check(not (APP / rel).exists(), f"removed {rel}")
    check(status_key() is None, "status key removed")
    check(not PROTECTED.exists(), "protected helper folder removed")
    check(not rules_present(), "no filter of Nox's provider left")


NODE_TASK = "Nox Node"
SCHTASKS = Path(wfp_helper.system_dir()) / "schtasks.exe"
AUMID_KEY = "Software\\Classes\\AppUserModelId\\Nox.Node"
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def node_task_xml() -> str | None:
    result = subprocess.run(
        [str(SCHTASKS), "/Query", "/TN", NODE_TASK, "/XML"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def aumid_present() -> bool:
    try:
        winreg.CloseKey(
            winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, AUMID_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            )
        )
    except FileNotFoundError:
        return False
    return True


def check_node_installed() -> None:
    from xml.etree import ElementTree  # noqa: PLC0415 - Task Scheduler's own XML

    for rel in ("runtime/pythonw.exe", "app/nox/__init__.py", "app/start_node.pyw", "nox-node.cmd"):
        check((APP / rel).is_file(), f"node mode installed {rel}")
    check(not (APP / "app" / "ui").exists(), "node mode installs no dashboards")
    xml = node_task_xml()
    check(xml is not None, f"the logon task {NODE_TASK!r} exists")
    assert xml is not None
    task = ElementTree.fromstring(xml)  # noqa: S314 - written by schtasks on this runner
    trigger = task.find(f"{TASK_NS}Triggers/{TASK_NS}LogonTrigger")
    principal = task.find(f"{TASK_NS}Principals/{TASK_NS}Principal")
    check(trigger is not None and bool(trigger.findtext(f"{TASK_NS}UserId")), "at logon of a user")
    assert principal is not None
    print(xml, flush=True)  # noqa: T201 - the task as Windows stored it, for the log
    # schtasks leaves out values at their default: no RunLevel element means LeastPrivilege.
    level = principal.findtext(f"{TASK_NS}RunLevel")
    check(level in (None, "LeastPrivilege"), f"least privilege ({level})")
    logon = principal.findtext(f"{TASK_NS}LogonType")
    check(logon == "InteractiveToken", f"as the logged-in user ({logon})")
    check("S-1-5-18" not in xml and "HighestAvailable" not in xml, "never SYSTEM, never elevated")
    check("<Arguments>-I " in xml, "the task starts its Python isolated (-I)")
    check(aumid_present(), "the node's notification ID (HKCU, 64-bit view)")
    selftest = subprocess.run(
        [
            str(APP / "runtime" / "python.exe"),
            "-I",
            str(APP / "app" / "start_node.pyw"),
            "--selftest",
        ],
        cwd=APP / "app",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    print(selftest.stdout, selftest.stderr, sep="\n", flush=True)  # noqa: T201
    check(selftest.returncode == 0 and "Not paired." in selftest.stdout, "the node starts")


def check_node_removed() -> None:
    check(not (APP / "runtime").exists() and not (APP / "app").exists(), "node files removed")
    check(node_task_xml() is None, "the logon task is gone")
    check(not aumid_present(), "the notification ID is gone")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("setup", type=Path)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    check(wfp_helper.helper_sha256() is not None and BUILT_HELPER.is_file(), "built helper")
    check(not APP.exists() and status_key() is None, "a clean machine to start from")

    step("1/6 fresh install")
    install(args.setup, args.logs / "install.log")
    first = check_installed()

    step("2/6 install over itself (upgrade path)")
    install(args.setup, args.logs / "upgrade.log")
    second = check_installed()
    check(second > first, f"the rule step ran again (sequence {first} -> {second})")

    step("3/6 uninstall")
    uninstall(args.logs / "uninstall.log")
    check_removed()

    node = ("/MODE=node", "/MERGETASKS=!sandboxrules")
    step("4/6 node mode refused when Setup was elevated on purpose (UAC on)")
    if uac_on():
        log = args.logs / "node-elevated.log"
        code = run_setup(args.setup, log, *node)
        check(code != 0, f"elevated node-mode setup ended with an error ({code}), not a dialog")
        check("Node mode refused" in log_tail(log, 400), "the refusal is in the setup log")
        check(not (APP / "app").exists(), "nothing was installed")
    else:
        step("UAC is off on this machine: there is no boundary, nothing to refuse")

    # A standard user's token: what a person gets who starts Setup normally while UAC is on.
    # Setup, its log and its temp files live in this account's own folders, which that token may
    # write (the runner's work folder may need the administrators' rights).
    step("5/6 node mode as a standard user: logon task, no dashboards, no elevated step")
    own = Path(os.environ["TEMP"]) / "nox-node-smoke"
    own.mkdir(parents=True, exist_ok=True)
    local_setup = own / args.setup.name
    shutil.copy2(args.setup, local_setup)
    local_log = own / "node.log"
    try:
        install(local_setup, local_log, *node, limited=True)
    finally:
        if local_log.exists():
            shutil.copy2(local_log, args.logs / "node.log")
    check_node_installed()
    check(status_key() is None, "node mode without the rules task asked for no elevation")

    step("6/6 node uninstall")
    uninstall(args.logs / "node-uninstall.log")
    check_node_removed()
    step("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
