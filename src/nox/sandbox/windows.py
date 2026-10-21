"""The Windows sandbox backend: a confined, hidden, resource-capped child process.

Two variants, strongest first:

* **AppContainer** (default when the profile can be created, which needs no administrator): the
  child runs as a container SID with no capabilities, so it cannot read the user's files, cannot
  reach the credential store, and has no internet or LAN access. The work directory is ACL'd to the
  container SID so that is the one place it can read and write. Whether it can reach a *loopback*
  port of the host depends on Windows network isolation, which is not enforced on every machine
  (measured: on some Windows 11 builds a desktop AppContainer reaches any host loopback listener).
  The installer's one elevated step therefore adds network rules (`nox.sandbox.wfp`: WFP filters
  per container SID) that block everything for every Nox container except the egress
  forwarder's own loopback ports. Either way the backend measures the result
  (`host_loopback_reachable`, fail closed: anything but an explicit "blocked" report counts as
  reachable, and so does a host listener in the forwarder's port range) and reports
  `confines_network` honestly: where loopback
  leaks, local services (a model server, a browser's debug port) would be reachable from inside, and
  the sandbox is not "strong". To run Python (a skill's tests, an MCP server written in Python),
  Nox's container SID additionally gets *read and execute* on exactly the Python runtime Nox itself
  runs on: the interpreter's install folder (python.exe, DLLs, standard library) and the virtual
  environment (`pyvenv.cfg`, `Scripts`, site-packages) - `python_runtime_dirs()`. Never a user
  folder as such, never a folder that contains Nox's home (secrets, database) or lies inside it;
  such a candidate is skipped and the child simply cannot start Python. Only Nox's own container SID
  is named (never ALL APPLICATION PACKAGES, which would open the folders to every packaged app on
  the machine). Every ACE Nox sets is recorded in a manifest (`<nox home>/sandbox/acl-grants.json`)
  so a restart adds no duplicate and `nox sandbox reset` - also run by the uninstaller - removes
  exactly those ACEs again (`RuntimeGrants`). Measured on Windows 11: while such a container-SID ACE
  sits on an interpreter folder, that interpreter fails to load its DLL under the *low-integrity*
  token (STATUS_DLL_NOT_FOUND). So the low-integrity backend resets the recorded grants when it
  starts; the two backends never share a machine state.
* **Low-integrity token** (fallback): the child runs with a low-integrity copy of the core's token.
  Mandatory integrity control stops it *writing* anything labelled medium (every user file). It
  does **not** stop it reading the user's files or the credential store, and integrity level does
  nothing to sockets - a low-IL process reaches the LAN and the internet like any other. Its
  `Guarantees` say exactly that, it never runs anything that declared egress (there is nothing that
  could hold it to the proxy), and `Sandbox` refuses it altogether unless the user explicitly
  accepted the weaker mode (`hands.accept_weak_sandbox`).

One container per workload class: a shell command, each MCP server and each skill run in an
AppContainer of their own (`Nox.Sandbox` for shell commands, one slot of a fixed pool
`Nox.Sandbox.w000`... for each other class, `nox.sandbox.containers`), so concurrent runs of
different workloads cannot read each other's work folders or open each other's processes - a work
folder is granted to its own run's container SID only, and runtime grants are recorded per SID
(`nox sandbox reset` removes them all). Runs of the same class share a container.

Network with AppContainer: a run that declares egress gets the proxy over a named pipe whose DACL
names only the container SID, plus a small forwarder started *inside the same container* that
listens on the container's own `127.0.0.1:<port>` (a port of `FORWARDER_PORTS`, the one range the
network rules leave open) and relays into the pipe. The child's `HTTPS_PROXY` points at that port.
Loopback to its own container is allowed under network isolation, and the pipe does not depend on
it at all - so declared egress works on machines that enforce loopback isolation, on those that do
not, and with the network rules in place, and still goes only through the proxy.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nox.core.logging import get_logger
from nox.sandbox.containers import (
    DEFAULT_WORKLOAD,
    SHELL_CONTAINER,
    ContainerSlots,
    container_name,
)
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)
from nox.sandbox.process import ProcessIo
from nox.sandbox.wfp import FORWARDER_PORTS, ports_by_sid

if TYPE_CHECKING:
    from nox.sandbox._win32 import Spawned

log = get_logger(__name__)

__all__ = [
    "FORWARDER_PS",
    "RuntimeGrants",
    "WindowsSandbox",
    "appcontainer_available",
    "forwarder_script",
    "default_grants_manifest",
    "forwarder_range_listeners",
    "host_loopback_reachable",
    "nox_runtime_dirs",
    "package_import_root",
    "probe_verdict",
    "python_runtime_dirs",
]

_APP_CONTAINER_NAME = SHELL_CONTAINER


_SYSTEM32 = r"C:\Windows\System32"
_POWERSHELL = _SYSTEM32 + r"\WindowsPowerShell\v1.0\powershell.exe"
#: Environment the child inherits. Nothing that points at the user's profile or secrets.
_SAFE_ENV_KEYS = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "PATHEXT",
    "ComSpec",
    "OS",
)
#: How long the in-container forwarder may take to report its port before the run fails closed.
_FORWARDER_READY_S = 20.0
_READ_CHUNK = 64 * 1024

#: The in-container egress forwarder: listen on the container's own loopback, on a free port of
#: `FORWARDER_PORTS` (`{lo}`-`{hi}`, tried from a random start; the network rules leave only that
#: range open), and relay each connection into the proxy's named pipe. `{pipe}` is the pipe's
#: short name. It blocks in
#: `Task.WaitAny` on the pending accept and every relay until one of them finishes - no polling,
#: no idle wake-ups (a PowerShell script block cannot run as a callback on a pool thread, so the
#: .NET tasks do the I/O and the script only waits for them).
FORWARDER_PS = r"""
$ErrorActionPreference = 'Stop'
$pipe = '{pipe}'
$lo = {lo}; $n = {hi} - $lo + 1
$start = [System.Random]::new().Next($n)
$l = $null
for ($i = 0; $i -lt $n -and $null -eq $l; $i++) {
  $t = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,
    $lo + (($start + $i) % $n))
  $t.ExclusiveAddressUse = $true
  try { $t.Start(); $l = $t } catch { }
}
if ($null -eq $l) { [Console]::Out.WriteLine('NOXFWD NONE'); exit 1 }
[Console]::Out.WriteLine('NOXFWD ' + $l.LocalEndpoint.Port)
[Console]::Out.Flush()
$open = [System.Collections.ArrayList]::new()
$accept = $l.AcceptTcpClientAsync()
while ($true) {
  $waits = [System.Collections.Generic.List[System.Threading.Tasks.Task]]::new()
  $waits.Add($accept)
  foreach ($x in $open) { $waits.Add($x[2]); $waits.Add($x[3]) }
  [void][System.Threading.Tasks.Task]::WaitAny($waits.ToArray())
  if ($accept.IsCompleted) {
    if ($accept.IsFaulted) { exit 1 }
    $c = $accept.Result
    $accept = $l.AcceptTcpClientAsync()
    try {
      $p = [System.IO.Pipes.NamedPipeClientStream]::new('.', $pipe,
        [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::Asynchronous)
      $p.Connect(5000)
      $n = $c.GetStream()
      [void]$open.Add(@($c, $p, $n.CopyToAsync($p), $p.CopyToAsync($n)))
    } catch { $c.Close() }
  }
  foreach ($x in @($open)) {
    if ($x[2].IsCompleted -or $x[3].IsCompleted) {
      $x[1].Dispose(); $x[0].Close(); [void]$open.Remove($x)
    }
  }
}
"""


def forwarder_script(pipe: str, ports: range) -> str:
    """`FORWARDER_PS` for the pipe `pipe` (its short name) and its container's own ports."""
    if not ports or ports.start not in FORWARDER_PORTS or ports.stop - 1 not in FORWARDER_PORTS:
        raise ValueError(f"not a forwarder port block: {ports}")
    return (
        FORWARDER_PS.replace("{pipe}", pipe)
        .replace("{lo}", str(ports.start))
        .replace("{hi}", str(ports.stop - 1))
    )


def forwarder_port(line: str, ports: range) -> int | None:
    """The port in the forwarder's ready line - only one of its container's `ports`, else `None`."""
    parts = line.split()
    if len(parts) != 2 or parts[0] != "NOXFWD" or not parts[1].isdigit():
        return None
    port = int(parts[1])
    return port if port in ports and port in FORWARDER_PORTS else None


def excluded_overlap(netsh_output: str, ports: range) -> list[tuple[int, int]]:
    """The reserved TCP port ranges (`netsh interface ipv4 show excludedportrange protocol=tcp`,
    any display language: every line of two numbers) that overlap `ports`."""
    found: list[tuple[int, int]] = []
    for line in netsh_output.splitlines():
        parts = line.replace("*", " ").split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            lo, hi = int(parts[0]), int(parts[1])
            if lo <= ports.stop - 1 and hi >= ports.start:
                found.append((lo, hi))
    return found


def _excluded_ranges(ports: range) -> list[tuple[int, int]] | None:
    """Windows' reserved port ranges over `ports`, or `None` if they cannot be read."""
    try:
        out = subprocess.run(
            [_SYSTEM32 + r"\netsh.exe", "interface", "ipv4", "show", "excludedportrange", "tcp"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return excluded_overlap(out.stdout, ports) if out.returncode == 0 else None


def no_forwarder_port_reason(ports: range, excluded: list[tuple[int, int]] | None) -> str:
    """Why the forwarder found no free port - honestly, Windows' own reservations included."""
    block = f"{ports.start}-{ports.stop - 1}"
    if excluded:
        ranges = ", ".join(f"{lo}-{hi}" for lo, hi in excluded)
        return (
            f"the egress forwarder found no free port in its block {block}: Windows reserves "
            f"{ranges} (an excluded port range, usually taken by Hyper-V, WSL or Docker at boot; "
            "`netsh interface ipv4 show excludedportrange protocol=tcp` lists them). Declared "
            "network access is unavailable until that reservation moves - often after a "
            "restart - and the run is refused (failing closed)"
        )
    unread = "" if excluded is not None else ", or reserved - the reservations could not be read"
    return (
        f"the egress forwarder found no free port in its block {block} (every port in use"
        f"{unread}); the run is refused (failing closed)"
    )


def container_forwarder_ports(sid: str) -> range:
    """The forwarder ports the network rules open for container `sid` - its block alone."""
    ports = ports_by_sid().get(sid)
    if ports is None:
        raise SandboxUnavailable("the run's container has no forwarder ports; failing closed")
    return ports


def appcontainer_available() -> bool:
    try:
        from nox.sandbox import _win32

        _win32.app_container_sid(_APP_CONTAINER_NAME)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "use the fallback"
        log.info("sandbox.appcontainer_unavailable", error=f"{type(exc).__name__}: {exc}")
        return False


_loopback_probe: dict[str, bool] = {}
_probe_lock = threading.Lock()
#: What the probe child prints after its connection attempt failed. Only this, with exit code 0
#: and no connection seen by the core, counts as "isolated".
_PROBE_BLOCKED = "NOXPROBE BLOCKED"
#: Plain .NET, no cmdlet: a cmdlet that cannot be loaded inside the container (module autoload
#: can fail there) would throw too, and a catch-all would read that as "blocked" - fail open. Only
#: a refused connection (`SocketException`) is "blocked"; anything else is inconclusive.
_PROBE_PS = (
    "try {{ [System.Net.Sockets.TcpClient]::new().Connect('127.0.0.1', {port}); "
    "[Console]::Out.WriteLine('NOXPROBE OPEN') }} "
    "catch [System.Net.Sockets.SocketException] {{ [Console]::Out.WriteLine('"
    + _PROBE_BLOCKED
    + "') }} "
    "catch {{ [Console]::Out.WriteLine('NOXPROBE ERROR') }}; exit 0"
)


def host_loopback_reachable(sid: str) -> bool:
    """Whether a child in AppContainer `sid` can connect to a loopback port of the host.

    The core listens on `127.0.0.1:0`, a contained PowerShell tries to connect and reports what
    happened, and the core watches for the connection. Fail closed: only an explicit "blocked"
    report from a probe that ran to its end (exit code 0) and no connection seen counts as
    isolated. A timeout, a crash, a missing report or a probe that could not start counts as
    reachable, and stays so for this process like any measured result: were it measured again, a
    later "blocked" could flip the answer between `guarantees()` and the check a run makes, so
    one call would refuse what the next one allows. One answer per process and container; a
    restart measures again.

    A "blocked" report is not enough on its own: the network rules (`nox.sandbox.wfp`) leave the
    egress forwarder's port range open on `127.0.0.1`, so a *host* program listening in that range
    would be reachable from inside. While anything listens there when the probe runs (or the
    listeners cannot be listed), the answer is "reachable" too.
    """
    with _probe_lock:
        if sid in _loopback_probe:
            return _loopback_probe[sid]
        measured = _probe_loopback(sid)
        if measured is False:
            in_range = forwarder_range_listeners()
            if in_range != []:
                log.warning("sandbox.forwarder_range_in_use", ports=in_range)
                measured = True
        log.info("sandbox.loopback_probe", reachable=measured, conclusive=measured is not None)
        # An inconclusive probe counts as reachable and stays so for this process: re-measuring
        # could flip the answer between `guarantees()` and the next run's check, and the weaker
        # reading must be the one that holds (fail closed). A restart measures again.
        reachable = True if measured is None else measured
        _loopback_probe[sid] = reachable
        return reachable


#: The egress forwarders this process started and has not released yet, as (pid, creation time):
#: their listeners are Nox's own. A pid alone could be reused by another process after a
#: forwarder ended; the pair cannot.
_forwarders: set[tuple[int, float]] = set()
_forwarders_lock = threading.Lock()
#: How often a running run's forwarder ports are checked again (`_PortWatch`).
_WATCH_INTERVAL_S = 1.5


def _identity(pid: int | None) -> tuple[int, float] | None:
    """(pid, creation time) of a live process, `None` when it cannot be read (counts as foreign)."""
    if pid is None:
        return None
    import psutil

    try:
        return pid, psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def _release_forwarder(fwd: Spawned, identity: tuple[int, float] | None = None) -> None:
    if identity is not None:
        with _forwarders_lock:
            _forwarders.discard(identity)
    fwd.close()
    _close_fds(fwd)


def forwarder_port_problem(port: int, own: tuple[int, float] | None, ports: range) -> str | None:
    """`None` when `port` is held by the forwarder `own` (pid, creation time) and every other
    listener on its container's `ports` is one of Nox's own live forwarders; otherwise why not.

    Checked before every run that uses the forwarder and again every `_WATCH_INTERVAL_S` while it
    runs (`_PortWatch`): the network rules permit the container's whole block, so a foreign
    listener there - or a port the forwarder does not actually hold - would be reachable from
    inside. A listener whose owner cannot be identified counts as foreign, and so does a listener
    table that cannot be read at all (fail closed)."""
    import psutil

    if own is None:
        return "the forwarder process cannot be identified"
    try:
        conns = psutil.net_connections(kind="tcp")
    except (OSError, psutil.Error) as exc:
        return f"the listeners cannot be listed: {type(exc).__name__}"
    with _forwarders_lock:
        ours = set(_forwarders) | {own}
    held = False
    foreign: set[int] = set()
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port not in ports:
            continue
        who = own if c.pid == own[0] else _identity(c.pid)
        if c.laddr.port == port and who == own:
            held = True
        elif c.laddr.port == port or who not in ours:
            foreign.add(c.laddr.port)
    if foreign:
        return f"a listener that is not Nox's forwarder holds port(s) {sorted(foreign)}"
    if not held:
        return f"port {port} is not held by the forwarder"
    return None


class _PortWatch:
    """Re-checks a running run's forwarder ports every `_WATCH_INTERVAL_S` and ends the run
    (`stop()`) as soon as a foreign listener appears there or the forwarder loses its port - so a
    listener that starts after the run began is not reachable for longer than one interval."""

    def __init__(
        self,
        port: int,
        own: tuple[int, float],
        ports: range,
        stop: Callable[[], None],
        interval_s: float = _WATCH_INTERVAL_S,
    ) -> None:
        self.problem: str | None = None
        self._args = (port, own, ports)
        self._stop = stop
        self._interval = interval_s
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="nox-sbx-portwatch", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._done.wait(self._interval):
            problem = forwarder_port_problem(*self._args)
            if problem is not None:
                self.problem = problem
                log.warning("sandbox.forwarder_port_taken", reason=problem)
                with contextlib.suppress(Exception):
                    self._stop()
                return

    def close(self) -> None:
        self._done.set()


def forwarder_range_listeners() -> list[int] | None:
    """The ports of `FORWARDER_PORTS` something on this computer listens on; `None` when the
    listeners cannot be listed (which the probe reads as "in use": fail closed)."""
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (OSError, psutil.Error) as exc:
        log.warning("sandbox.listeners_unknown", error=f"{type(exc).__name__}: {exc}")
        return None
    return sorted(
        {
            c.laddr.port
            for c in conns
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port in FORWARDER_PORTS
        }
    )


def probe_verdict(*, connected: bool, result: Result | None) -> bool | None:
    """Reachable (True), isolated (False) or inconclusive (None) from one probe run."""
    if connected:
        return True
    if result is None or result.timed_out or result.exit_code != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if lines == [_PROBE_BLOCKED]:
        return False
    if lines == ["NOXPROBE OPEN"]:
        return True  # it connected somewhere, even if the core's listener did not see it
    return None


def _probe_listener() -> socket.socket:
    """A socket bound to a free `127.0.0.1` port outside `FORWARDER_PORTS` - the range the network
    rules leave open, so a probe there would say nothing about the rest of loopback."""
    for _ in range(16):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        if sock.getsockname()[1] not in FORWARDER_PORTS:
            return sock
        sock.close()
    raise OSError("no free loopback port outside the forwarder range")


def _probe_loopback(sid: str) -> bool | None:
    from nox.sandbox import _win32

    try:
        listener = _probe_listener()
    except OSError as exc:
        log.warning("sandbox.loopback_probe_failed", error=str(exc))
        return None
    listener.listen(1)
    listener.settimeout(8.0)
    port = listener.getsockname()[1]
    connected = threading.Event()

    def accept() -> None:
        try:
            conn, _ = listener.accept()
            connected.set()
            conn.close()
        except OSError:
            pass

    watcher = threading.Thread(target=accept, daemon=True)
    watcher.start()
    argv = [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _PROBE_PS.format(port=port)]
    work = _long_path(Path(tempfile.mkdtemp(prefix="nox-sbx-probe-")))
    try:
        _grant_container(work, sid)
        spawned = _win32.spawn(
            argv,
            cwd=str(work),
            env=_base_env(work),
            memory_bytes=256 * 1024 * 1024,
            max_processes=4,
            app_container=sid,
        )
    except OSError as exc:
        log.warning("sandbox.loopback_probe_failed", error=str(exc))
        listener.close()
        _best_effort_rmtree(work)
        return None
    result: Result | None = None
    try:
        result = _collect(spawned, Limits(timeout_s=30.0, max_output_bytes=4096))
        watcher.join(timeout=2.0)
    except OSError as exc:
        log.warning("sandbox.loopback_probe_failed", error=str(exc))
    finally:
        spawned.close()
        _close_fds(spawned)
        listener.close()
        _best_effort_rmtree(work)
    return probe_verdict(connected=connected.is_set(), result=result)


#: LocalSystem: kept on a work folder so the system can clean it up like any temporary folder.
_SYSTEM_SID = "S-1-5-18"
#: What Python's `mkdtemp` (0o700) or an inherited ACL may put on a fresh folder besides the
#: user: removed, so the folder names exactly its user, SYSTEM and one container.
_STRIP_SIDS = ("S-1-5-32-544", "S-1-3-4", "S-1-5-32-545", "S-1-5-11", "S-1-15-2-1")


def _work_folder_dacl(work: Path, sid: str, user_sid: str) -> list[list[str]]:
    """The icacls calls that give `work` exactly this DACL, inheritance removed: the core's user
    (full control), SYSTEM (full control) and the run's container SID (full control).

    An AppContainer child passes a *dual* access check: its token's user and groups must be
    granted, and its container SID too. A folder that names only the container SID is therefore
    unusable by the container itself (measured on the hosted Windows runner, where the core runs
    elevated and a fresh folder's owner is Administrators); one that names the user but another
    container SID is unusable by this one - which is the isolation between workloads.
    """
    icacls = _SYSTEM32 + r"\icacls.exe"
    grant = [f"*{who}:(OI)(CI)F" for who in (user_sid, _SYSTEM_SID, sid)]
    return [
        [icacls, str(work), "/inheritance:r", "/grant:r", *grant],
        [icacls, str(work), "/remove:g", *(f"*{who}" for who in _STRIP_SIDS)],
    ]


def _grant_container(work: Path, sid: str) -> None:
    """Give `work` its DACL (`_work_folder_dacl`) - or fail closed: a folder the container cannot
    use is a run that fails later, with a misleading "access denied"."""
    from nox.sandbox import _win32

    for argv in _work_folder_dacl(work, sid, _win32.current_user_sid()):
        done = subprocess.run(  # noqa: S603 - fixed argv, our own temp path
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined,unused-ignore]
        )
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()[:1]
            raise SandboxUnavailable(
                f"could not give the sandbox its work folder (icacls {done.returncode}: "
                f"{detail[0] if detail else 'no output'})"
            )


def _long_path(path: Path) -> Path:
    """`path` with every 8.3 component expanded (`RUNNER~1` -> `runneradmin`)."""
    try:
        return path.resolve(strict=True)
    except OSError:
        return path


def _base_env(work: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env["PATH"] = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0"
    work_s = str(work)
    env["TEMP"] = env["TMP"] = work_s
    env["USERPROFILE"] = env["LOCALAPPDATA"] = env["APPDATA"] = work_s
    # PowerShell writes a module cache; keep it in the work dir, not the (unwritable) profile.
    env["PSModuleAnalysisCachePath"] = str(work / "psmac")
    env["PSModulePath"] = _module_path(env.get("SystemRoot", r"C:\Windows"))
    return env


def _module_path(system_root: str) -> str:
    r"""The container's `PSModulePath`: the system's own modules first, by their long path.

    Never the user's profile folder. Windows PowerShell 5.1 puts
    `%ProgramFiles%\WindowsPowerShell\Modules` back into any list that lacks it - *in front of*
    `$PSHOME\Modules`. Command lookup then analysed every third-party module there before the
    system's own, and on a host with many of them (the hosted CI runner: AWS, Microsoft.Graph and
    one folder the container may not read) it gave up before it reached `Join-Path`: no cmdlet
    could be found at all. Named here, last, the folder stays where it is put, so every built-in
    cmdlet resolves from System32 before a third-party module is ever read.
    """
    system = rf"{system_root}\System32\WindowsPowerShell\v1.0\Modules"
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return rf"{system};{program_files}\WindowsPowerShell\Modules"


def _contains(outer: Path, inner: Path) -> bool:
    return inner == outer or inner.is_relative_to(outer)


Runner = Callable[[list[str]], int]
#: One explicit (not inherited) allow ACE of a folder: SID, access mask, inheritance flags.
Ace = tuple[str, int, int]
AclReader = Callable[[Path], list[Ace]]
#: ALL APPLICATION PACKAGES: never granted any more; only removed where an old build put it.
_LEGACY_BROAD_SID = "S-1-15-2-1"
#: The exact ACE the old build set: ReadAndExecute + Synchronize, container and object inherit.
_LEGACY_ACE = (_LEGACY_BROAD_SID, 0x1200A9, 0x3)
#: Lists a folder's explicit allow ACEs with SIDs (never localized account names); the path comes
#: through the environment, so no quoting of a user path can break the command. Plain .NET, not
#: `Get-Acl`: a module autoload can fail under an inherited PowerShell 7 `PSModulePath`.
_ACL_PS = (
    "$r = ([System.IO.DirectoryInfo]::new($env:NOX_ACL_PATH)).GetAccessControl().GetAccessRules("
    "$true, $false, "
    "[System.Security.Principal.SecurityIdentifier]); foreach ($a in $r) { if ("
    "[int]$a.AccessControlType -eq 0) { '{0}|{1}|{2}' -f $a.IdentityReference.Value, "
    "[int]$a.FileSystemRights, [int]$a.InheritanceFlags } }"
)


def _icacls(argv: list[str]) -> int:
    done = subprocess.run(  # noqa: S603 - fixed argv
        argv,
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined,unused-ignore]
    )
    return done.returncode


def _explicit_aces(folder: Path) -> list[Ace]:
    """The explicit allow ACEs on `folder` (inherited ones are not the folder's own)."""
    done = subprocess.run(  # noqa: S603 - fixed argv; the path travels in the environment
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", _ACL_PS],
        capture_output=True,
        text=True,
        encoding="ascii",
        errors="replace",
        check=False,
        env={**os.environ, "NOX_ACL_PATH": str(folder)},
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined,unused-ignore]
    )
    if done.returncode != 0:
        raise OSError(f"could not read the ACL of {folder}")
    aces: list[Ace] = []
    for line in done.stdout.splitlines():
        sid, _, rest = line.strip().partition("|")
        mask, _, flags = rest.partition("|")
        if sid and mask.isdigit() and flags.isdigit():
            aces.append((sid, int(mask), int(flags)))
    return aces


def default_grants_manifest() -> Path:
    from nox.paths import nox_home

    return nox_home() / "sandbox" / "acl-grants.json"


class RuntimeGrants:
    """The read ACEs Nox puts on runtime folders for its AppContainer - recorded, reversible.

    `grant(folder, sid)` adds `*SID:(OI)(CI)(RX)` once and records it, together with the folder's
    state *before* Nox's first grant (`broad_before`: whether it already carried an explicit
    `ALL APPLICATION PACKAGES` ACE); a recorded pair is never granted again. `reset()` removes
    exactly the recorded ACEs (`icacls /remove:g`) and empties the manifest. `migrate_legacy()`
    cleans what one earlier build left behind, and touches nothing Nox cannot prove it put there.
    The runner and the ACL reader are injectable so tests can assert the exact commands.
    """

    def __init__(
        self,
        manifest: Path | None = None,
        *,
        runner: Runner = _icacls,
        acl_reader: AclReader = _explicit_aces,
    ) -> None:
        self.manifest = manifest or default_grants_manifest()
        self._run = runner
        self._read_acl = acl_reader

    def _entries(self) -> list[dict[str, Any]]:
        import json

        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("grants", []) if isinstance(data, dict) else []
        return [
            {"path": str(item["path"]), "sid": str(item["sid"]), **_pre_state(item)}
            for item in entries
            if isinstance(item, dict) and "path" in item and "sid" in item
        ]

    def recorded(self) -> list[tuple[str, str]]:
        return [(entry["path"], entry["sid"]) for entry in self._entries()]

    def _write(self, entries: list[dict[str, Any]]) -> None:
        import json

        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(json.dumps({"grants": entries}, indent=2), encoding="utf-8")

    def grant(self, folder: Path, sid: str) -> bool:
        if not sid.startswith("S-1-15-2-") or sid == _LEGACY_BROAD_SID:
            raise ValueError("only Nox's own container SID is ever granted")
        entries = self._entries()
        if any(e["path"] == str(folder) and e["sid"] == sid for e in entries):
            return True
        try:
            broad_before: bool | None = any(
                ace[0] == _LEGACY_BROAD_SID for ace in self._read_acl(folder)
            )
        except OSError:
            broad_before = None
        ok = self._run(["icacls", str(folder), "/grant", f"*{sid}:(OI)(CI)(RX)", "/Q"]) == 0
        if ok:
            # `None` would read as "pre-state unknown" (a manifest of the build right after the
            # legacy one); a grant Nox makes now always knows it, so an unreadable ACL counts as
            # "was already there" - the stricter reading: the migration never touches it.
            pre = True if broad_before is None else broad_before
            self._write([*entries, {"path": str(folder), "sid": sid, "broad_before": pre}])
        return ok

    def migrate_legacy(self, *, force: bool = False) -> int:
        """Remove the broad `ALL APPLICATION PACKAGES` read ACE one earlier build put on the
        runtime folders - and only where Nox can tell it did (review finding #18).

        That build granted `*S-1-15-2-1:(OI)(CI)(RX)` and recorded nothing; the build right after
        it recorded its own grants without the folder's prior state. So a folder is cleaned only
        when (a) Nox recorded a grant on it, (b) that record predates pre-state recording
        (`broad_before` unknown), and (c) the folder carries exactly the legacy ACE, explicitly.
        A folder whose recorded pre-state says the ACE was already there, a folder Nox never
        granted, an inherited ACE and an ACE of any other shape are never touched. Once per
        installation (a marker next to the manifest) unless `force`; returns how many folders
        were cleaned.
        """
        marker = self.manifest.with_name("legacy-acl-migrated")
        if marker.exists() and not force:
            return 0
        entries = self._entries()
        cleaned = 0
        changed = False
        for entry in entries:
            if entry["broad_before"] is not None or not Path(entry["path"]).exists():
                continue
            try:
                aces = self._read_acl(Path(entry["path"]))
            except OSError:
                continue  # unknown stays unknown: try again next time
            broad = [ace for ace in aces if ace[0] == _LEGACY_BROAD_SID]
            if broad != [_LEGACY_ACE]:
                # None, or some other shape: not provably Nox's - leave it, and know it from now on.
                entry["broad_before"] = bool(broad)
                changed = True
            elif (
                self._run(["icacls", entry["path"], "/remove:g", f"*{_LEGACY_BROAD_SID}", "/Q"])
                == 0
            ):
                cleaned += 1
                entry["broad_before"] = False
                changed = True
            # A failed removal stays unknown, so `nox sandbox reset` (force) tries it again.
        if changed:
            self._write(entries)
        log.info("sandbox.legacy_acl_migrated", folders=cleaned)
        with contextlib.suppress(OSError):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("done\n", encoding="utf-8")
        return cleaned

    def reset(self) -> int:
        """Remove every recorded ACE; returns how many were removed. Failures stay recorded."""
        left: list[dict[str, Any]] = []
        removed = 0
        for entry in self._entries():
            if not Path(entry["path"]).exists():
                continue  # the folder is gone, and its ACE with it
            if self._run(["icacls", entry["path"], "/remove:g", f"*{entry['sid']}", "/Q"]) == 0:
                removed += 1
            else:
                left.append(entry)
        if left:
            self._write(left)
        else:
            with contextlib.suppress(OSError):
                self.manifest.unlink()
        return removed


def _pre_state(item: dict[str, Any]) -> dict[str, bool | None]:
    value = item.get("broad_before")
    return {"broad_before": value if isinstance(value, bool) else None}


def python_runtime_dirs(
    *, home: Path | None = None, base: str | None = None, prefix: str | None = None
) -> tuple[Path, ...]:
    """The folders an AppContainer child must read to run this Python, minus any unsafe one.

    Unsafe: a drive root, the user's profile folder itself, or a folder that contains Nox's home
    or lies inside it - granting read there would hand the container the secrets file and the
    database.
    """
    from nox.paths import nox_home

    nox = (home or nox_home()).resolve()
    node = _node_home()
    profile = Path.home().resolve()
    # Never a broad profile folder: the profile itself, anything above it, or a folder directly
    # in it (Documents, AppData, ...). A runtime lives deeper (AppData/Roaming/uv/python/...).
    out: list[Path] = []
    for raw in (base or sys.base_prefix, prefix or sys.prefix):
        folder = Path(raw).resolve()
        if folder in out:
            continue
        if len(folder.parts) <= 1 or _contains(folder, profile) or folder.parent == profile:
            log.warning("sandbox.runtime_dir_refused", reason="root or broad profile folder")
            continue
        if (
            _contains(folder, nox)
            or _contains(nox, folder)
            or _contains(folder, node)
            or _contains(node, folder)
        ):
            log.warning("sandbox.runtime_dir_refused", reason="overlaps Nox's home")
            continue
        out.append(folder)
    return tuple(out)


def _node_home() -> Path:
    """A node's own folder (key, certificates, audit trail): never granted to a container."""
    from nox.node.home import default_home  # noqa: PLC0415

    return Path(os.path.realpath(default_home()))


def _runs_python(argv: list[str]) -> bool:
    """Whether `argv` starts an executable from Nox's own Python runtime (`python_runtime_dirs`)."""
    if not argv:
        return False
    try:
        exe = Path(argv[0]).resolve()
    except (OSError, ValueError):
        return False
    return any(_contains(folder, exe) for folder in python_runtime_dirs())


def package_import_root() -> Path:
    """The folder `import nox` resolves from: site-packages, or `src` in an editable install."""
    import nox

    return Path(nox.__file__).resolve().parent.parent


def nox_runtime_dirs(*, home: Path | None = None, root: Path | None = None) -> tuple[Path, ...]:
    """`python_runtime_dirs()` plus the import root of the `nox` package itself, when it is not
    inside them already (an editable install keeps it in the checkout's `src`).

    A self-built skill's worker imports `nox.plugins.api` inside the container, so it needs read
    access to Nox's own source - code, never data. The same refusals apply as for the runtime.
    """
    from nox.paths import nox_home

    dirs = python_runtime_dirs(home=home)
    folder = (root or package_import_root()).resolve()
    nox = (home or nox_home()).resolve()
    profile = Path.home().resolve()
    if any(_contains(d, folder) for d in dirs):
        return dirs
    if len(folder.parts) <= 1 or _contains(folder, profile) or folder.parent == profile:
        log.warning("sandbox.package_dir_refused", reason="root or broad profile folder")
        return dirs
    if _contains(folder, nox) or _contains(nox, folder) or _contains(folder, _node_home()):
        log.warning("sandbox.package_dir_refused", reason="overlaps Nox's home")
        return dirs
    return (*dirs, folder)


class WindowsSandbox:
    """Backend for `nox.sandbox.Sandbox` on Windows. One instance per core."""

    def __init__(
        self,
        *,
        prefer_appcontainer: bool = True,
        grants: RuntimeGrants | None = None,
        slots: ContainerSlots | None = None,
    ) -> None:
        from nox.sandbox import _win32

        self._w = _win32
        self._sid: str | None = None
        if prefer_appcontainer and appcontainer_available():
            self._sid = _win32.app_container_sid(_APP_CONTAINER_NAME)
        self.mode = SandboxMode.WINDOWS_APPCONTAINER if self._sid else SandboxMode.WINDOWS_LOW_IL
        #: The proxy reaches an AppContainer child over a named pipe (see the module docstring).
        self.proxy_transport = "pipe" if self._sid else "none"
        #: `Sandbox` passes each run's workload class to this backend (`isolates_workloads`).
        self.isolates_workloads = True
        #: Container SID per workload class (`container_name`); created on first use.
        self._sids: dict[str, str] = {DEFAULT_WORKLOAD: self._sid} if self._sid else {}
        #: Which pool container each workload class owns (`nox.sandbox.containers`).
        self._slots = slots or ContainerSlots()
        #: Container SIDs whose Python runtime grants are in place.
        self._runtime_ready: set[str] = set()
        self._grants = grants or RuntimeGrants()
        self._grants.migrate_legacy()
        if self._sid is None and self._grants.recorded():
            # Container-SID ACEs on the interpreter break low-IL Python (module docstring).
            removed = self._grants.reset()
            log.info("sandbox.runtime_grants_reset", removed=removed)

    @property
    def container_sid(self) -> str | None:
        """The container SID of shell commands (the default workload)."""
        return self._sid

    def container_sid_for(self, workload: str) -> str | None:
        """The container SID of `workload`'s class, creating its profile on first use; `None` in
        the low-integrity fallback."""
        if self._sid is None:
            return None
        key = workload or DEFAULT_WORKLOAD
        sid = self._sids.get(key)
        if sid is None:
            try:
                sid = self._w.app_container_sid(container_name(key, self._slots))
            except OSError as exc:
                raise SandboxUnavailable(f"no AppContainer for {key!r}: {exc}") from exc
            self._sids[key] = sid
        return sid

    def grant_read(self, folder: Path, sid: str | None = None) -> bool:
        """Give a container SID (default: shell's) read+execute on `folder` - recorded, once
        per SID (`RuntimeGrants`)."""
        target = sid or self._sid
        if target is None:
            return False
        return self._grants.grant(folder, target)

    async def _ensure_runtime(self, argv: list[str], sid: str | None) -> None:
        """Make the Python runtime readable for the run's container (see above) - once per
        container SID, and only when a run actually starts that Python: `cmd`, PowerShell or curl
        need no access to it, so their runs leave the runtime's ACL alone."""
        if sid is None or sid in self._runtime_ready or not _runs_python(argv):
            return
        self._runtime_ready.add(sid)
        for folder in nox_runtime_dirs():
            ok = await asyncio.to_thread(self.grant_read, folder, sid)
            log.info("sandbox.runtime_readable", ok=ok)

    def guarantees(self) -> Guarantees:
        if self._sid is not None:
            leaks = host_loopback_reachable(self._sid)
            notes = ["AppContainer: no file, credential, internet or LAN capability"]
            if leaks:
                notes.append(
                    "Windows does not enforce loopback isolation for the container on this "
                    "machine: services listening on this computer's loopback are reachable "
                    "from inside the sandbox. The sandbox network rules close this: the "
                    "installer adds them (it asks for administrator rights once); run setup "
                    "again to add them later"
                )
            return Guarantees(
                backend=self.mode.value,
                confines_writes=True,
                confines_reads=True,
                hides_secrets=True,
                confines_network=not leaks,
                notes=tuple(notes),
            )
        return Guarantees(
            backend=self.mode.value,
            confines_writes=True,
            confines_reads=False,
            hides_secrets=False,
            confines_network=False,
            notes=(
                "low-integrity fallback: cannot write the user's files, but CAN read them and the "
                "credential store, and CAN open network connections (integrity level does not "
                "restrict sockets); runs that declare network access are refused",
            ),
        )

    def _prepare_workdir(self, cwd: str | None, sid: str | None) -> tuple[Path, bool]:
        # Always the long path: an 8.3 name in it (`C:\Users\RUNNER~1`, a TEMP set that way)
        # is expanded by listing each parent folder, which a container may not do - the child
        # would be refused its own work folder.
        if cwd is not None:
            return _long_path(Path(cwd)), False
        work = _long_path(Path(tempfile.mkdtemp(prefix="nox-sbx-")))
        if sid is not None:
            # Only this run's container SID gets its one writable place: a run of another
            # workload class cannot read or write it.
            _grant_container(work, sid)
        else:
            self._w.label_low(str(work))
        return work, True

    def _child_env(
        self,
        work: Path,
        proxy_port: int | None,
        extra: Mapping[str, str] | None = None,
        proxy_auth: str = "",
    ) -> dict[str, str]:
        # The caller's extra variables first, so the sandbox's own always win (they are also
        # refused up front by `checked_env`).
        env = {**(extra or {}), **_base_env(work)}
        if proxy_port is not None:
            userinfo = f"{proxy_auth}@" if proxy_auth else ""
            url = f"http://{userinfo}127.0.0.1:{proxy_port}"
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = env["ALL_PROXY"] = url
            env["https_proxy"] = env["http_proxy"] = env["all_proxy"] = url
        return env

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,  # noqa: ARG002 - the proxy is reached over `proxy_socket` here
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
        workload: str = DEFAULT_WORKLOAD,
    ) -> Result:
        """Run `argv` confined, in the container of `workload`'s class. With egress,
        `proxy_socket` is the proxy's named pipe; a low-integrity run with egress is refused -
        nothing could hold it to the proxy."""
        self._check_egress(egress, proxy_socket)
        sid = self.container_sid_for(workload)
        await self._ensure_runtime(argv, sid)
        work, created = self._prepare_workdir(cwd, sid)
        forwarder: Spawned | None = None
        identity: tuple[int, float] | None = None
        watch: _PortWatch | None = None
        try:
            port: int | None = None
            ports = range(0)
            if egress.wants_network and proxy_socket:
                forwarder, port, identity, ports = await asyncio.to_thread(
                    self._start_forwarder, work, proxy_socket, limits, sid
                )
            env = self._child_env(work, port, extra_env, proxy_auth)
            try:
                spawned = self._w.spawn(
                    argv,
                    cwd=str(work),
                    env=env,
                    memory_bytes=limits.memory_bytes,
                    max_processes=limits.max_processes,
                    app_container=sid,
                )
            except OSError as exc:
                raise SandboxUnavailable(f"could not start the sandboxed process: {exc}") from exc
            try:
                if port is not None and identity is not None:
                    watch = _PortWatch(port, identity, ports, spawned.kill)
                result = await asyncio.to_thread(_collect, spawned, limits)
                if watch is not None and watch.problem is not None:
                    raise SandboxUnavailable(
                        f"the run was ended: {watch.problem} while it ran (failing closed)"
                    )
                return result
            finally:
                if watch is not None:
                    watch.close()
                spawned.close()
                _close_fds(spawned)
        finally:
            if forwarder is not None:
                _release_forwarder(forwarder, identity)
            if created:
                _best_effort_rmtree(work)

    def _check_egress(self, egress: EgressDecl, proxy_socket: str | None) -> None:
        if not egress.wants_network:
            return
        if self._sid is None:
            raise SandboxUnavailable(
                "the low-integrity Windows fallback cannot confine network access, so a run "
                "that needs the network is refused (AppContainer is required for egress)"
            )
        if not proxy_socket:
            raise SandboxUnavailable("egress declared but no proxy pipe is open; failing closed")

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,  # noqa: ARG002 - the proxy is reached over `proxy_socket` here
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
        workload: str = DEFAULT_WORKLOAD,
    ) -> ProcessIo:
        """Start `argv` confined exactly like `run`, but keep it running with stdio pipes.

        With egress, the in-container forwarder lives exactly as long as the process. The pipes
        are blocking anonymous pipes, so each one is served by its own dedicated thread (`_Lane`)
        instead of the shared default executor: a long-running server parks a read for hours,
        and doing that in `asyncio.to_thread` would starve every other user of the pool.
        """
        self._check_egress(egress, proxy_socket)
        sid = self.container_sid_for(workload)
        await self._ensure_runtime(argv, sid)
        work, created = self._prepare_workdir(cwd, sid)
        forwarder: Spawned | None = None
        identity: tuple[int, float] | None = None
        watch: list[_PortWatch] = []

        def drop_forwarder() -> None:
            for w in watch:
                w.close()
            if forwarder is not None:
                _release_forwarder(forwarder, identity)

        try:
            port: int | None = None
            ports = range(0)
            if egress.wants_network and proxy_socket:
                forwarder, port, identity, ports = await asyncio.to_thread(
                    self._start_forwarder, work, proxy_socket, limits, sid
                )
            env = self._child_env(work, port, extra_env, proxy_auth)
            spawned = self._w.spawn(
                argv,
                cwd=str(work),
                env=env,
                memory_bytes=limits.memory_bytes,
                max_processes=limits.max_processes,
                with_stdin=True,
                app_container=sid,
            )
        except BaseException as exc:
            drop_forwarder()
            if created:
                _best_effort_rmtree(work)
            if isinstance(exc, OSError):
                raise SandboxUnavailable(f"could not start the sandboxed process: {exc}") from exc
            raise
        if spawned.stdin_fd is None:  # requested with_stdin=True; never None in practice
            spawned.close()
            drop_forwarder()
            raise SandboxUnavailable("the sandboxed process has no stdin pipe")
        if port is not None and identity is not None:
            # A long-running server: its forwarder's ports are watched for as long as it lives;
            # a foreign listener there ends it (the caller sees the process exit).
            watch.append(_PortWatch(port, identity, ports, spawned.kill))
        stdin_fd: list[int | None] = [spawned.stdin_fd]
        lanes = {name: _Lane(f"nox-sbx-{spawned.pid}-{name}") for name in ("out", "err", "in")}
        waiter = _Lane(f"nox-sbx-{spawned.pid}-wait")

        async def write_in(data: bytes) -> None:
            fd = stdin_fd[0]
            if fd is None:
                raise BrokenPipeError("stdin is closed")
            await lanes["in"].call(_write_all, fd, data)

        def close_in() -> None:
            fd, stdin_fd[0] = stdin_fd[0], None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)

        async def read_out(n: int) -> bytes:
            data: bytes = await lanes["out"].call(_read, spawned.stdout_fd, n)
            return data

        async def read_err(n: int) -> bytes:
            data: bytes = await lanes["err"].call(_read, spawned.stderr_fd, n)
            return data

        async def wait() -> int | None:
            code: int | None = await waiter.call(spawned.wait, self._w.INFINITE)
            return code

        async def cleanup() -> None:
            close_in()
            for fd in (spawned.stdout_fd, spawned.stderr_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)
            spawned.close()
            drop_forwarder()
            for lane in (*lanes.values(), waiter):
                lane.close()
            if created:
                await asyncio.to_thread(_best_effort_rmtree, work)

        return ProcessIo(
            pid=spawned.pid,
            read_out=read_out,
            read_err=read_err,
            write_in=write_in,
            close_in=close_in,
            wait=wait,
            kill=spawned.kill,
            cleanup=cleanup,
        )

    def _start_forwarder(
        self, work: Path, pipe_path: str, limits: Limits, sid: str | None
    ) -> tuple[Spawned, int, tuple[int, float], range]:
        """Start the in-container forwarder (in the run's own container) and return it with the
        loopback port it listens on, its (pid, creation time) and its container's port block."""
        assert sid is not None
        short = pipe_path.rsplit("\\", 1)[-1]
        if not short.replace("-", "").isalnum():
            raise SandboxUnavailable("unexpected proxy pipe name; failing closed")
        ports = container_forwarder_ports(sid)
        script = forwarder_script(short, ports)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            fwd = self._w.spawn(
                [_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                cwd=str(work),
                env=_base_env(work),
                memory_bytes=min(limits.memory_bytes, 256 * 1024 * 1024),
                max_processes=2,
                app_container=sid,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"could not start the egress forwarder: {exc}") from exc
        line = _read_line(fwd.stdout_fd, _FORWARDER_READY_S)
        port = forwarder_port(line, ports)
        if port is None:
            fwd.close()
            _close_fds(fwd)
            if line.strip() == "NOXFWD NONE":
                reason = no_forwarder_port_reason(ports, _excluded_ranges(ports))
                log.warning("sandbox.forwarder_no_port", ports=f"{ports.start}-{ports.stop - 1}")
                raise SandboxUnavailable(reason)
            raise SandboxUnavailable("the egress forwarder did not start; failing closed")
        own = _identity(fwd.pid)
        problem = forwarder_port_problem(port, own, ports)
        if problem is not None or own is None:
            fwd.close()
            _close_fds(fwd)
            log.warning("sandbox.forwarder_unverified", port=port, reason=problem)
            raise SandboxUnavailable(
                f"the egress forwarder could not be verified ({problem}); the network rules "
                "leave the forwarder's port range open, so the run is refused rather than given "
                "a path to a listener that is not Nox's (failing closed)"
            )
        with _forwarders_lock:
            _forwarders.add(own)
        return fwd, port, own, ports


class _Lane:
    """One dedicated daemon thread that runs blocking calls in order, for one pipe or wait.

    `call()` hands a function to the thread and awaits its result. A call that is cancelled on
    the loop side still finishes on the thread (a blocking read cannot be interrupted); its
    result is dropped. `close()` lets the thread end after the call it is in.
    """

    def __init__(self, name: str) -> None:
        self._jobs: queue.SimpleQueue[
            tuple[Callable[..., Any], tuple[Any, ...], asyncio.Future[Any]] | None
        ] = queue.SimpleQueue()
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._serve, name=name, daemon=True)
        self._thread.start()

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        future: asyncio.Future[Any] = self._loop.create_future()
        self._jobs.put((fn, args, future))
        return await future

    def close(self) -> None:
        self._jobs.put(None)

    def _serve(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, args, future = job
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiting coroutine
                self._settle(future, None, exc)
            else:
                self._settle(future, result, None)

    def _settle(self, future: asyncio.Future[Any], result: Any, exc: BaseException | None) -> None:
        def apply() -> None:
            if future.done():
                return
            if exc is not None:
                future.set_exception(exc)
            else:
                future.set_result(result)

        with contextlib.suppress(RuntimeError):  # the loop is already closed
            self._loop.call_soon_threadsafe(apply)


def _read(fd: int, n: int) -> bytes:
    try:
        return os.read(fd, n)
    except BrokenPipeError:  # the write end is gone: that is end of output, not an error
        return b""


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_line(fd: int, timeout_s: float) -> str:
    """The first line `fd` produces within `timeout_s`, or "" (reads on a helper thread)."""
    data = bytearray()
    done = threading.Event()

    def reader() -> None:
        try:
            while b"\n" not in data:
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                data.extend(chunk)
        except OSError:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    done.wait(timeout_s)
    return data.split(b"\n", 1)[0].decode("ascii", errors="replace").strip()


class _Pump(threading.Thread):
    """Drains one pipe to EOF, keeping at most `cap` bytes; the rest is read and dropped so the
    child never blocks on a full pipe (review finding 10)."""

    def __init__(self, fd: int, cap: int) -> None:
        super().__init__(daemon=True)
        self.fd = fd
        self.cap = cap
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, _READ_CHUNK)
            except OSError:
                return
            if not chunk:
                return
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.truncated = True


def _collect(spawned: Spawned, limits: Limits) -> Result:
    """Wait for the run while draining stdout and stderr concurrently; kill on timeout."""
    cap = limits.max_output_bytes
    pumps = [_Pump(spawned.stdout_fd, cap), _Pump(spawned.stderr_fd, cap)]
    for pump in pumps:
        pump.start()
    exit_code = spawned.wait(int(limits.timeout_s * 1000))
    timed_out = exit_code is None
    # Kill what is left of the tree either way: on timeout the run itself, after a normal exit any
    # background process it left holding the pipes open.
    spawned.kill()
    for pump in pumps:
        pump.join(timeout=5.0)
    out, err = pumps
    return Result(
        exit_code=exit_code,
        stdout=bytes(out.data).decode("utf-8", errors="replace"),
        stderr=bytes(err.data).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=out.truncated or err.truncated,
    )


def _close_fds(spawned: object) -> None:
    for name in ("stdin_fd", "stdout_fd", "stderr_fd"):
        fd = getattr(spawned, name, None)
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError:
            pass
        setattr(spawned, name, None)


def _best_effort_rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
