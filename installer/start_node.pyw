"""Start Nox as a node and keep it running: `pythonw.exe start_node.pyw` (Windows logon task),
`python start_node.pyw` (systemd unit, launchd agent).

Standard library only, and never replaced by a self-update: this file decides which copy of Nox
runs, so it must not be part of what it chooses between. The copies:

* the **base** - the folder this file is in (`nox/`, `config/` next to it), put there by the
  installer;
* **releases** a node installed itself from a signed bundle (`nox.node.update`), in
  `<node home>/app/releases/<version>`, described by `<node home>/app/state.json`.

Choosing, at every start: a `pending` release is tried (at most `MAX_TRIALS` times); otherwise the
`current` release; otherwise - or when the base is at least as new, after a new installer ran -
the base. The node runs as a child process. It exits with `RESTART_EXIT_CODE` when a new version
is in place, which starts it again at once. A pending release whose start fails before it
confirmed itself (connected to the core, or ran long enough) is rolled back: the previous copy
runs again, and that version is marked bad and never tried again. A copy that keeps crashing is
restarted with a growing pause, and given up after `MAX_CRASHES_PER_HOUR`.

Other uses:
  start_node.pyw --cli node pair <link>   run a `nox` command with the copy that would run
  start_node.pyw --autostart on|off       Windows: the per-user logon task (never elevated)
  start_node.pyw --start                  Windows: start that task now (the Start-menu entry)
  start_node.pyw --selftest               print the version of the copy that would run

Nothing here ever asks for elevation; the logon task runs as the user who registered it, with
"least privilege".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

_HERE = Path(__file__).resolve().parent
#: The installer's copy: next to this file (Windows payload), or `src/` of the checkout this file
#: is part of (Linux and macOS installs, `installer/start_node.pyw` in a release folder).
BASE = _HERE if (_HERE / "nox").is_dir() else _HERE.parent / "src"
RESTART_EXIT_CODE = 75
MAX_TRIALS = 2
MAX_CRASHES_PER_HOUR = 10
TASK_NAME = "Nox Node"
HOME_ENV = "NOX_NODE_HOME"
APP_ENV = "NOX_NODE_APP"
_VERSION = re.compile(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)[^"]*"', re.MULTILINE)
_CREATE_NO_WINDOW = 0x08000000


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


def read_state(root: Path) -> dict:  # type: ignore[type-arg]
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(root: Path, state: dict) -> None:  # type: ignore[type-arg]
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "state.json.new"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / "state.json")


def _release(root: Path, name: object) -> Path | None:
    if not isinstance(name, str) or not re.fullmatch(r"[0-9A-Za-z.-]{1,60}", name):
        return None
    folder = root / "releases" / name
    return folder if version_of(folder) is not None else None


def rollback(root: Path, version: str) -> None:
    """The pending `version` failed to start: back to what ran before, never this one again."""
    state = read_state(root)
    if state.get("pending") != version:
        return
    bad = state.get("bad") if isinstance(state.get("bad"), list) else []
    state.update(pending=None, attempts=0, bad=[*bad, version][-50:])
    write_state(root, state)
    shutil.rmtree(root / "releases" / version, ignore_errors=True)


def choose(base: Path, root: Path, *, trial: bool = True) -> tuple[Path, str | None]:
    """The copy to start, and the pending version it tries (None when it is not a trial).
    `trial=False` (a one-off command) never starts or counts a trial."""
    state = read_state(root)
    floor = version_of(base) or (0, 0, 0)
    pending = state.get("pending")
    folder = _release(root, pending) if trial else None
    if trial and isinstance(pending, str) and folder is None:
        rollback(root, pending)  # its folder is gone or broken: nothing to try
    if folder is not None and isinstance(pending, str):
        attempts = state.get("attempts") if isinstance(state.get("attempts"), int) else 0
        if attempts >= MAX_TRIALS:
            rollback(root, pending)
        else:
            state["attempts"] = attempts + 1
            write_state(root, state)
            return folder, pending
    current = _release(root, state.get("current"))
    if current is not None and (version_of(current) or floor) > floor:
        return current, None
    return base, None


def run_in_process(app: Path, args: list[str]) -> int:
    """`nox <args>` with the code in `app`, in this process."""
    sys.path.insert(0, str(app))
    from nox.cli import main  # type: ignore[import-not-found]  # noqa: PLC0415

    sys.argv = ["nox", *args]
    try:
        main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def _run_child(app: Path) -> int:
    env = {**os.environ, APP_ENV: str(app), "NOX_NODE_LAUNCHER": "1"}
    return subprocess.call(  # noqa: S603 - this interpreter and this file
        [sys.executable, "-s", str(Path(__file__).resolve()), "--cli", "node", "run"],
        env=env,
        creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


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
        code = run_child(app)
        if code == RESTART_EXIT_CODE:
            continue
        confirmed = trial is None or read_state(root).get("pending") != trial
        if not confirmed and code != 0:
            rollback(root, trial)  # type: ignore[arg-type]  # trial is set when not confirmed
            continue
        if code == 0:
            return 0
        now = clock()
        crashes = [t for t in crashes if now - t < 3600] + [now]
        if len(crashes) >= MAX_CRASHES_PER_HOUR:
            report(f"Nox stopped after {len(crashes)} failed starts within an hour (exit {code}).")
            return 1
        sleep(pause)
        pause = min(pause * 2, 60.0)


# ---- the Windows logon task -------------------------------------------------------------------


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
    """Register (or remove) the logon task for the user running this - no elevation."""
    if sys.platform != "win32":
        print("Autostart on Linux and macOS is set up by the installer.", file=sys.stderr)  # noqa: T201
        return 2
    flags = {"capture_output": True, "creationflags": _CREATE_NO_WINDOW, "check": False}
    if not enable:
        result = subprocess.run([_schtasks(), "/Delete", "/TN", TASK_NAME, "/F"], **flags)  # noqa: S603
        return 0 if result.returncode in (0, 1) else result.returncode
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    xml = task_xml(
        user=f"{domain}\\{user}" if domain else user,
        command=str(pythonw if pythonw.is_file() else sys.executable),
        arguments=f'"{Path(__file__).resolve()}"',
        workdir=str(_HERE),
    )
    definition = node_home().parent / "nox-node-task.xml"
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_text(xml, encoding="utf-16")
    try:
        result = subprocess.run(  # noqa: S603 - the system's schtasks, fixed arguments
            [_schtasks(), "/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F"], **flags
        )
    finally:
        definition.unlink(missing_ok=True)
    if result.returncode != 0:
        report(f"The logon task could not be registered: {result.stderr!r}")
    return int(result.returncode)


# ---- reporting --------------------------------------------------------------------------------


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
