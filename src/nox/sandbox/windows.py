"""The Windows sandbox backend: a confined, hidden, resource-capped child process.

Two variants, strongest first:

* **AppContainer** (default when the profile can be created, which needs no administrator): the
  child runs as a container SID with no capabilities, so it cannot read the user's files, cannot
  reach the credential store, and has no internet or LAN access. The work directory is ACL'd to the
  container SID so that is the one place it can read and write. Whether it can reach a *loopback*
  port of the host depends on Windows network isolation, which is not enforced on every machine
  (measured: on some Windows 11 builds a desktop AppContainer reaches any host loopback listener).
  So the backend measures it (`host_loopback_reachable`, fail closed: anything but an explicit
  "blocked" report counts as reachable) and reports `confines_network` honestly: where loopback
  leaks, local services (a model server, a browser's debug port) would be reachable from inside, and
  the sandbox is not "strong". To run Python (a skill's tests, an MCP server written in Python), the
  container SID additionally gets *read and execute* on exactly the Python runtime Nox itself runs
  on: the interpreter's install folder (python.exe, DLLs, standard library) and the virtual
  environment (`pyvenv.cfg`, `Scripts`, site-packages) - `python_runtime_dirs()`. Never a user
  folder as such, never a folder that contains Nox's home (secrets, database) or lies inside it;
  such a candidate is skipped and the child simply cannot start Python.
* **Low-integrity token** (fallback): the child runs with a low-integrity copy of the core's token.
  Mandatory integrity control stops it *writing* anything labelled medium (every user file). It
  does **not** stop it reading the user's files or the credential store, and integrity level does
  nothing to sockets - a low-IL process reaches the LAN and the internet like any other. Its
  `Guarantees` say exactly that, it never runs anything that declared egress (there is nothing that
  could hold it to the proxy), and `Sandbox` refuses it altogether unless the user explicitly
  accepted the weaker mode (`hands.accept_weak_sandbox`).

Network with AppContainer: a run that declares egress gets the proxy over a named pipe whose DACL
names only the container SID, plus a small forwarder started *inside the same container* that
listens on the container's own `127.0.0.1:<port>` and relays into the pipe. The child's
`HTTPS_PROXY` points at that port. Loopback to its own container is allowed under network
isolation, and the pipe does not depend on it at all - so declared egress works on machines that
enforce loopback isolation and on those that do not, and still goes only through the proxy.
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
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)
from nox.sandbox.process import ProcessIo

if TYPE_CHECKING:
    from nox.sandbox._win32 import Spawned

log = get_logger(__name__)

__all__ = [
    "FORWARDER_PS",
    "WindowsSandbox",
    "appcontainer_available",
    "host_loopback_reachable",
    "probe_verdict",
    "python_runtime_dirs",
]

_APP_CONTAINER_NAME = "Nox.Sandbox"
_SYSTEM32 = r"C:\Windows\System32"
_POWERSHELL = _SYSTEM32 + r"\WindowsPowerShell\v1.0\powershell.exe"
_CURL = _SYSTEM32 + r"\curl.exe"
#: ALL APPLICATION PACKAGES - see the module docstring for why not the container's own SID.
_ALL_APP_PACKAGES = "S-1-15-2-1"
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

#: The in-container egress forwarder: listen on the container's own loopback, relay each
#: connection into the proxy's named pipe. `{pipe}` is the pipe's short name. Polls instead of
#: using callbacks because a PowerShell script block cannot run on a thread-pool thread.
FORWARDER_PS = r"""
$ErrorActionPreference = 'Stop'
$pipe = '{pipe}'
$l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$l.Start()
[Console]::Out.WriteLine('NOXFWD ' + $l.LocalEndpoint.Port)
[Console]::Out.Flush()
$open = [System.Collections.ArrayList]::new()
while ($true) {
  while ($l.Pending()) {
    $c = $l.AcceptTcpClient()
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
  Start-Sleep -Milliseconds 10
}
"""


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
_PROBE_PS = (
    "try {{ (New-Object Net.Sockets.TcpClient).Connect('127.0.0.1', {port}); "
    "[Console]::Out.WriteLine('NOXPROBE OPEN') }} "
    "catch {{ [Console]::Out.WriteLine('" + _PROBE_BLOCKED + "') }}; exit 0"
)


def host_loopback_reachable(sid: str) -> bool:
    """Whether a child in AppContainer `sid` can connect to a loopback port of the host.

    The core listens on `127.0.0.1:0`, a contained PowerShell tries to connect and reports what
    happened, and the core watches for the connection. Fail closed: only an explicit "blocked"
    report from a probe that ran to its end (exit code 0) and no connection seen counts as
    isolated. A timeout, a crash, a missing report or a probe that could not start counts as
    reachable - and such an inconclusive result is not cached, so a later call measures again
    instead of keeping a guess. A conclusive result is measured once per process and container.
    """
    with _probe_lock:
        if sid in _loopback_probe:
            return _loopback_probe[sid]
        measured = _probe_loopback(sid)
        log.info("sandbox.loopback_probe", reachable=measured, conclusive=measured is not None)
        if measured is None:
            return True
        _loopback_probe[sid] = measured
        return measured


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


def _probe_loopback(sid: str) -> bool | None:
    from nox.sandbox import _win32

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
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
    work = Path(tempfile.mkdtemp(prefix="nox-sbx-probe-"))
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


def _grant_container(work: Path, sid: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, our own temp path
        [_SYSTEM32 + r"\icacls.exe", str(work), "/grant", f"*{sid}:(OI)(CI)F"],
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined,unused-ignore]
    )


def _base_env(work: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env["PATH"] = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0"
    work_s = str(work)
    env["TEMP"] = env["TMP"] = work_s
    env["USERPROFILE"] = env["LOCALAPPDATA"] = env["APPDATA"] = work_s
    # PowerShell writes a module cache; keep it in the work dir, not the (unwritable) profile.
    env["PSModuleAnalysisCachePath"] = str(work / "psmac")
    return env


def _contains(outer: Path, inner: Path) -> bool:
    return inner == outer or inner.is_relative_to(outer)


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
    profile = Path.home().resolve()
    out: list[Path] = []
    for raw in (base or sys.base_prefix, prefix or sys.prefix):
        folder = Path(raw).resolve()
        if folder in out:
            continue
        if len(folder.parts) <= 1 or folder == profile:
            log.warning("sandbox.runtime_dir_refused", reason="root or profile folder")
            continue
        if _contains(folder, nox) or _contains(nox, folder):
            log.warning("sandbox.runtime_dir_refused", reason="overlaps Nox's home")
            continue
        out.append(folder)
    return tuple(out)


class WindowsSandbox:
    """Backend for `nox.sandbox.Sandbox` on Windows. One instance per core."""

    def __init__(self, *, prefer_appcontainer: bool = True) -> None:
        from nox.sandbox import _win32

        self._w = _win32
        self._sid: str | None = None
        if prefer_appcontainer and appcontainer_available():
            self._sid = _win32.app_container_sid(_APP_CONTAINER_NAME)
        self.mode = SandboxMode.WINDOWS_APPCONTAINER if self._sid else SandboxMode.WINDOWS_LOW_IL
        #: The proxy reaches an AppContainer child over a named pipe (see the module docstring).
        self.proxy_transport = "pipe" if self._sid else "none"
        self._runtime_ready = False

    @property
    def container_sid(self) -> str | None:
        return self._sid

    def grant_read(self, folder: Path) -> bool:
        """Give AppContainers read+execute on `folder` (inherited by everything below).

        Additive and idempotent (`icacls /grant` merges an identical ACE); done once per backend,
        off the event loop.
        """
        if self._sid is None:
            return False
        done = subprocess.run(  # noqa: S603 - fixed argv, a vetted runtime folder
            ["icacls", str(folder), "/grant", f"*{_ALL_APP_PACKAGES}:(OI)(CI)(RX)", "/Q"],
            capture_output=True,
            check=False,
        )
        return done.returncode == 0

    async def _ensure_runtime(self) -> None:
        """Once per backend: make the Python runtime readable for the container (see above)."""
        if self._sid is None or self._runtime_ready:
            return
        self._runtime_ready = True
        for folder in python_runtime_dirs():
            ok = await asyncio.to_thread(self.grant_read, folder)
            log.info("sandbox.runtime_readable", ok=ok)

    def guarantees(self) -> Guarantees:
        if self._sid is not None:
            leaks = host_loopback_reachable(self._sid)
            notes = ["AppContainer: no file, credential, internet or LAN capability"]
            if leaks:
                notes.append(
                    "Windows does not enforce loopback isolation for the container on this "
                    "machine: services listening on this computer's loopback are reachable "
                    "from inside the sandbox"
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

    def _prepare_workdir(self, cwd: str | None) -> tuple[Path, bool]:
        if cwd is not None:
            return Path(cwd), False
        work = Path(tempfile.mkdtemp(prefix="nox-sbx-"))
        if self._sid is not None:
            # Grant the container SID full control of its one writable place.
            _grant_container(work, self._sid)
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
    ) -> Result:
        """Run `argv` confined. With egress, `proxy_socket` is the proxy's named pipe;
        a low-integrity run with egress is refused - nothing could hold it to the proxy."""
        self._check_egress(egress, proxy_socket)
        await self._ensure_runtime()
        work, created = self._prepare_workdir(cwd)
        forwarder: Spawned | None = None
        try:
            port: int | None = None
            if egress.wants_network and proxy_socket:
                forwarder, port = await asyncio.to_thread(
                    self._start_forwarder, work, proxy_socket, limits
                )
            env = self._child_env(work, port, extra_env, proxy_auth)
            try:
                spawned = self._w.spawn(
                    argv,
                    cwd=str(work),
                    env=env,
                    memory_bytes=limits.memory_bytes,
                    max_processes=limits.max_processes,
                    app_container=self._sid,
                )
            except OSError as exc:
                raise SandboxUnavailable(f"could not start the sandboxed process: {exc}") from exc
            try:
                return await asyncio.to_thread(_collect, spawned, limits)
            finally:
                spawned.close()
                _close_fds(spawned)
        finally:
            if forwarder is not None:
                forwarder.close()
                _close_fds(forwarder)
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
    ) -> ProcessIo:
        """Start `argv` confined exactly like `run`, but keep it running with stdio pipes.

        With egress, the in-container forwarder lives exactly as long as the process. The pipes
        are blocking anonymous pipes, so each one is served by its own dedicated thread (`_Lane`)
        instead of the shared default executor: a long-running server parks a read for hours,
        and doing that in `asyncio.to_thread` would starve every other user of the pool.
        """
        self._check_egress(egress, proxy_socket)
        await self._ensure_runtime()
        work, created = self._prepare_workdir(cwd)
        forwarder: Spawned | None = None

        def drop_forwarder() -> None:
            if forwarder is not None:
                forwarder.close()
                _close_fds(forwarder)

        try:
            port: int | None = None
            if egress.wants_network and proxy_socket:
                forwarder, port = await asyncio.to_thread(
                    self._start_forwarder, work, proxy_socket, limits
                )
            env = self._child_env(work, port, extra_env, proxy_auth)
            spawned = self._w.spawn(
                argv,
                cwd=str(work),
                env=env,
                memory_bytes=limits.memory_bytes,
                max_processes=limits.max_processes,
                with_stdin=True,
                app_container=self._sid,
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

    def _start_forwarder(self, work: Path, pipe_path: str, limits: Limits) -> tuple[Spawned, int]:
        """Start the in-container forwarder and return it with the loopback port it listens on."""
        assert self._sid is not None
        short = pipe_path.rsplit("\\", 1)[-1]
        if not short.replace("-", "").isalnum():
            raise SandboxUnavailable("unexpected proxy pipe name; failing closed")
        script = FORWARDER_PS.replace("{pipe}", short)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            fwd = self._w.spawn(
                [_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                cwd=str(work),
                env=_base_env(work),
                memory_bytes=min(limits.memory_bytes, 256 * 1024 * 1024),
                max_processes=2,
                app_container=self._sid,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"could not start the egress forwarder: {exc}") from exc
        line = _read_line(fwd.stdout_fd, _FORWARDER_READY_S)
        if not line.startswith("NOXFWD "):
            fwd.close()
            _close_fds(fwd)
            raise SandboxUnavailable("the egress forwarder did not start; failing closed")
        return fwd, int(line.split()[1])


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
