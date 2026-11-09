"""Start Nox as a node and keep it running: `pythonw.exe -I start_node.pyw` (Windows logon task),
`python -I start_node.pyw` (systemd unit, launchd agent).

Never replaced by a self-update: this file decides which copy of Nox runs, so it must not be part
of what it chooses between. The copies:

* the **base** - the folder this file is in (`nox/`, `config/` next to it), put there by the
  installer;
* **releases** a node installed itself from a signed bundle (`nox.node.update`), in
  `<node home>/app/releases/<version>`, named by `<node home>/app/state.json`.

The node's account can write the releases and `state.json`, so neither is trusted as it is. A
release runs only after it passed `nox.node.bundle.verify_folder` - imported from the *base*, with
the base's trusted release keys: the signed manifest, the bundle it lists, the SHA-256 of every
file and nothing else in the folder. Anything else, or a release not newer than the base, runs the
base and is dropped from `state.json` (a new installer always wins).

At every start: a `pending` release is tried; otherwise the `current` one; otherwise the base. The
node runs as a child process, isolated (`-I -B`, its own folder as the working folder, no
PYTHON* variables). It exits with `RESTART_EXIT_CODE` when a new version is in place, which
starts it again at once, and with `TRIAL_FAILED_EXIT_CODE` when the core refused the protocol of
a version on trial. Only real crashes count against a pending version - a non-zero exit without
the node's clean-exit marker, not a signal or a logoff. After `MAX_TRIALS` crashes, or a refused
protocol, the previous copy runs again and the version is skipped for a week. A copy that keeps
crashing is restarted with a growing pause and given up after `MAX_CRASHES_PER_HOUR`.

Other uses:
  start_node.pyw --cli node pair <link>   run a `nox` command with the copy that would run
  start_node.pyw --autostart on|off       Windows: the per-user logon task (never elevated)
  start_node.pyw --start                  Windows: start that task now (the Start-menu entry)
  start_node.pyw --reset-overlays         drop every self-installed version (the installers)
  start_node.pyw --selftest               print the version of the copy that would run

Nothing here ever asks for elevation; the logon task runs as the user who registered it, with
"least privilege".
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

_HERE = Path(__file__).resolve().parent
#: The installer's copy: next to this file (Windows payload), or `src/` of the checkout this file
#: is part of (Linux and macOS installs, `installer/start_node.pyw` in a release folder).
BASE = _HERE if (_HERE / "nox").is_dir() else _HERE.parent / "src"
RESTART_EXIT_CODE = 75
TRIAL_FAILED_EXIT_CODE = 76
MAX_TRIALS = 2
MAX_CRASHES_PER_HOUR = 10
TASK_NAME = "Nox Node"
HOME_ENV = "NOX_NODE_HOME"
APP_ENV = "NOX_NODE_APP"
#: Written by the node when it ends in order (`nox.node.cli.run`); removed before every start.
CLEAN_EXIT = "clean-exit"
_VERSION = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)[^"]*"', re.MULTILINE)
#: The only names a release folder may have (the same rule as `nox.node.update`).
_VERSION_DIR = re.compile(
    r"^\d{1,6}\.\d{1,6}\.\d{1,6}(?:-[0-9A-Za-z]{1,20}(?:\.[0-9A-Za-z]{1,20})*)?$"
)
#: Ended from outside, not crashed: SIGTERM, SIGINT, SIGHUP (a stop, Ctrl+C, a closed session -
#: as a negative return code, or 128+n from a shell), and on Windows a closed console, logoff or
#: shutdown and `schtasks /End`. Anything else - SIGKILL (the OOM killer), SIGABRT, SIGSEGV, any
#: other non-zero code - is a crash.
_TERMINATED = {-15, -2, -1, 143, 130, 129, 0xC000013A, 0x40010004, 0x41306}
_CREATE_NO_WINDOW = 0x08000000
#: How long a state.lock is waited for (another launcher, node or CLI holds it).
LOCK_WAIT_S = 120.0


def node_home() -> Path:
    """Where the node keeps its files - the same rule as `nox.node.home.default_home`."""
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Nox" / "node"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Nox" / "node"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "nox" / "node"


def version_of(app: Path) -> tuple[int, int, int] | None:
    try:
        match = _VERSION.search((app / "nox" / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


# ---- state.json, under the lock nox.node.update takes too ---------------------------------------


@contextlib.contextmanager
def _locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "state.lock").open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            handle.seek(0)
            pause, waited = 0.05, 0.0
            while True:  # LK_LOCK gives up after ~10 s: retry without a limit that short
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if waited >= LOCK_WAIT_S:
                        raise
                    time.sleep(pause)
                    waited += pause
                    pause = min(pause * 2, 1.0)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_state(root: Path) -> dict[str, Any]:
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(root: Path, state: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "state.json.new"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / "state.json")


@contextlib.contextmanager
def editing(root: Path) -> Iterator[dict[str, Any]]:
    with _locked(root):
        state = read_state(root)
        yield state
        write_state(root, state)


def _remove_release(root: Path, name: str) -> None:
    """Only ever a direct child of `releases/` with a version name - never `.`, `..` or a link."""
    if not _VERSION_DIR.match(name):
        return
    folder = root / "releases" / name
    if folder.is_symlink():
        folder.unlink()
    elif folder.parent == root / "releases":
        shutil.rmtree(folder, ignore_errors=True)


def mark_bad(root: Path, version: str, why: str) -> None:
    """The pending `version` failed: back to what ran before; not tried again for a week."""
    with editing(root) as state:
        if state.get("pending") != version:
            return
        bad = state.get("bad") if isinstance(state.get("bad"), dict) else {}
        bad[version] = dt.datetime.now(dt.UTC).isoformat()
        state.update(pending=None, failures=0, bad=bad)
    _remove_release(root, version)
    report(f"Nox {version} rolled back: {why}")


# ---- choosing ------------------------------------------------------------------------------------


def verified(folder: Path) -> bool:
    """The release folder is exactly a signed release - checked with the base's code and keys."""
    try:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        from nox.node.bundle import verify_folder  # type: ignore[import-not-found]  # noqa: PLC0415
        from nox.release import trusted_release_keys  # type: ignore[import-not-found]  # noqa: PLC0415

        verify_folder(folder, trusted_release_keys())
    except Exception as exc:  # noqa: BLE001 - every doubt runs the base instead
        report(f"{folder.name}: not started, it is not the signed release ({exc})")
        return False
    return True


def _candidate(root: Path, name: object, floor: tuple[int, int, int]) -> Path | None:
    if not isinstance(name, str) or not _VERSION_DIR.match(name):
        return None
    folder = root / "releases" / name
    found = version_of(folder)
    if found is None or found <= floor or not verified(folder):
        return None
    return folder


def choose(base: Path, root: Path, *, trial: bool = True) -> tuple[Path, str | None]:
    """The copy to start, and the pending version it tries (None when it is not a trial).
    `trial=False` (a one-off command) never starts a trial."""
    floor = version_of(base) or (0, 0, 0)
    # Checking a folder takes a moment (every file is hashed): done without the lock, then the
    # result is applied under it only if the state still names the same versions.
    seen = read_state(root)
    pending, current = seen.get("pending"), seen.get("current")
    pending_ok = pending is None or _candidate(root, pending, floor) is not None
    current_folder = _candidate(root, current, floor) if current is not None else None
    with editing(root) as state:
        if (state.get("pending"), state.get("current")) != (pending, current):
            return base, None  # changed meanwhile (an update was just installed): next start
        if not pending_ok:
            state.update(pending=None, failures=0)  # broken, unsigned, or not newer than the base
            pending = None
        if current is not None and current_folder is None:
            state["current"] = None
    if trial and isinstance(pending, str):
        return root / "releases" / pending, pending
    if current_folder is not None:
        return current_folder, None
    return base, None


def reset_overlays(root: Path) -> None:
    """A new base was installed: every self-installed version goes; the floor, the newest seen
    manifest and the failure record stay."""
    with editing(root) as state:
        for key in ("current", "previous", "pending"):
            state[key] = None
        state["failures"] = 0
        releases = root / "releases"
        if releases.is_symlink():
            releases.unlink()
        elif releases.is_dir():
            for folder in releases.iterdir():
                if folder.is_symlink():
                    folder.unlink()
                elif _VERSION_DIR.match(folder.name.removesuffix(".staging")):
                    shutil.rmtree(folder, ignore_errors=True)


# ---- running -------------------------------------------------------------------------------------


def run_in_process(app: Path, args: list[str]) -> int:
    """`nox <args>` with the code in `app`, in this process."""
    if app != BASE:
        # The check above imported the base's `nox`; the chosen copy must not mix with it.
        for name in [m for m in sys.modules if m == "nox" or m.startswith("nox.")]:
            del sys.modules[name]
    sys.path.insert(0, str(app))
    from nox.cli import main  # type: ignore[import-not-found]  # noqa: PLC0415

    sys.argv = ["nox", *args]
    try:
        main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def child_command(app: Path) -> tuple[list[str], dict[str, str], str]:
    """argv, environment and working folder of the node's process: this interpreter isolated
    (`-I`: no current folder or user site on sys.path, PYTHON* variables ignored - and removed;
    `-B`: no bytecode written into a release folder), this file by absolute path, the copy's own
    folder as the working folder."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update({APP_ENV: str(app), "NOX_NODE_LAUNCHER": "1"})
    argv = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--cli", "node", "run"]
    return argv, env, str(app)


def _run_child(app: Path) -> int:
    argv, env, cwd = child_command(app)
    return subprocess.call(  # noqa: S603 - this interpreter and this file
        argv,
        env=env,
        cwd=cwd,
        creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def crashed(code: int, root: Path) -> bool:
    """A real crash: not 0, not a restart, not ended from outside - and, for a positive code,
    the node did not mark an orderly end. A process killed by any other signal crashed."""
    if code in (0, RESTART_EXIT_CODE, TRIAL_FAILED_EXIT_CODE) or code in _TERMINATED:
        return False
    return code < 0 or not (root / CLEAN_EXIT).exists()


def supervise(
    base: Path,
    root: Path,
    *,
    run_child: Callable[[Path], int] = _run_child,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    crashes: list[float] = []
    pause = 2.0
    while True:
        app, trial = choose(base, root)
        (root / CLEAN_EXIT).unlink(missing_ok=True)
        code = run_child(app)
        if code == RESTART_EXIT_CODE:
            continue
        on_trial = trial is not None and read_state(root).get("pending") == trial
        if on_trial and code == TRIAL_FAILED_EXIT_CODE:
            mark_bad(root, trial, "the core does not accept its node protocol")  # type: ignore[arg-type]
            continue
        if not crashed(code, root):
            return 0  # ended in order, or ended from outside
        if on_trial:
            with editing(root) as state:
                failures = int(state.get("failures") or 0) + 1
                state["failures"] = failures
            if failures >= MAX_TRIALS:
                mark_bad(root, trial, f"it crashed {failures} times (exit {code})")  # type: ignore[arg-type]
                continue
        now = clock()
        crashes = [t for t in crashes if now - t < 3600] + [now]
        if len(crashes) >= MAX_CRASHES_PER_HOUR:
            report(f"Nox stopped after {len(crashes)} failed starts within an hour (exit {code}).")
            return 1
        sleep(pause)
        pause = min(pause * 2, 60.0)


# ---- the Windows logon task ----------------------------------------------------------------------


def task_xml(*, user: str, command: str, arguments: str, workdir: str) -> str:
    """At logon of `user`, as `user`, least privilege, no time limit, restarted after failures."""
    u, c, a, w = (escape(v) for v in (user, command, arguments, workdir))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Nox node: this PC as hands of the Nox at home. Never elevated.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{u}</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{u}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{c}</Command>
      <Arguments>{a}</Arguments>
      <WorkingDirectory>{w}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks() -> str:
    return str(Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "schtasks.exe")


def autostart(enable: bool) -> int:
    """Register (or remove) the logon task for the user running this - no elevation. The task
    definition goes through a folder created just for it, readable by this user only."""
    if sys.platform != "win32":
        print("Autostart on Linux and macOS is set up by the installer.", file=sys.stderr)  # noqa: T201
        return 2
    flags: dict[str, Any] = {"capture_output": True, "creationflags": _CREATE_NO_WINDOW}
    if not enable:
        result = subprocess.run(
            [_schtasks(), "/Delete", "/TN", TASK_NAME, "/F"], check=False, **flags
        )  # noqa: S603
        return 0 if result.returncode in (0, 1) else result.returncode
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    xml = task_xml(
        user=f"{domain}\\{user}" if domain else user,
        command=str(pythonw if pythonw.is_file() else sys.executable),
        arguments=f'-I "{Path(__file__).resolve()}"',
        workdir=str(_HERE),
    )
    with tempfile.TemporaryDirectory(prefix="nox-node-task-") as folder:
        definition = Path(folder) / "task.xml"
        with definition.open("x", encoding="utf-16") as handle:  # never an existing file
            handle.write(xml)
        result = subprocess.run(  # noqa: S603 - the system's schtasks, fixed arguments
            [_schtasks(), "/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F"],
            check=False,
            **flags,
        )
    if result.returncode != 0:
        report(f"The logon task could not be registered: {result.stderr!r}")
    return int(result.returncode)


# ---- reporting -----------------------------------------------------------------------------------


def report(text: str) -> None:
    """Into `<node home>/../node-launcher.log`, and on stderr (journal, launchd log)."""
    print(text, file=sys.stderr)  # noqa: T201
    try:
        log = node_home().parent / "node-launcher.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    root = node_home() / "app"
    try:
        if argv[:1] == ["--cli"]:
            given = os.environ.get(APP_ENV, "")
            app = Path(given) if given else choose(BASE, root, trial=False)[0]
            return run_in_process(app, argv[1:])
        if argv[:1] == ["--autostart"] and argv[1:2] in (["on"], ["off"]):
            return autostart(argv[1] == "on")
        if argv[:1] == ["--start"] and sys.platform == "win32":
            # Through the task: one node per user, whoever starts it (IgnoreNew).
            started = subprocess.run(  # noqa: S603 - the system's schtasks, fixed arguments
                [_schtasks(), "/Run", "/TN", TASK_NAME],
                capture_output=True,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
            return int(started.returncode)
        if argv[:1] == ["--reset-overlays"]:
            reset_overlays(root)
            return 0
        if argv[:1] == ["--selftest"]:
            app = choose(BASE, root, trial=False)[0]
            print(f"{'.'.join(map(str, version_of(app) or ()))} {app}")  # noqa: T201
            return run_in_process(app, ["node", "status"])
        if argv:
            print(__doc__, file=sys.stderr)  # noqa: T201
            return 2
        return supervise(BASE, root)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - pythonw has no console: every failure is written down
        report("".join(traceback.format_exception(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
