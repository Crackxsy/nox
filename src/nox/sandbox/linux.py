"""The Linux / Raspberry Pi sandbox backend: `bwrap` for isolation, `systemd-run` for limits.

`bubblewrap` (`bwrap`) builds the view the process gets: the system mounted read-only, the work
directory the only writable place, a private `/tmp` and `/dev`, no access to the user's home.

**Network.** Every run gets `--unshare-net`: a network namespace of its own that holds nothing but
its own loopback - no route to the internet, the LAN, the Home Assistant host or any service on
the host's loopback (abstract Unix sockets are per namespace too, so the host's D-Bus/X11 are out
of reach as well). That alone is "no egress declared".

Declared egress adds exactly one path out, and it is not a network path:

* the run's egress proxy (`EgressProxy.start_unix`) listens on a Unix domain socket in a private
  `0700` directory; `bwrap` binds that one socket file into the sandbox, read-only;
* the target is started by a tiny launcher (`_egress_forwarder.py`, run with the system's
  `python3 -I -S`) that listens on `127.0.0.1:EGRESS_PORT` - the *sandbox's* loopback - and relays
  every connection byte for byte to that socket path, nowhere else;
* `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` point at that port with the proxy's per-run credentials,
  which travel in the environment (never on a command line - `/proc/<pid>/cmdline` is readable
  by everyone).

So the child can reach the proxy and nothing else; the proxy, outside, checks the credentials,
the declaration and `NetGuard` (no loopback, LAN, link-local/metadata or Home Assistant). No root,
no firewall rule, no extra package beyond `bubblewrap` itself.

**Ceilings** (memory, processes) come from the first mechanism that really works here
(`Ceiling`): the service's delegated cgroup v2 subtree (`nox.service`, see `_cgroup`), a
`systemd-run --user --scope` of a desktop session's user manager, a system scope when running as
root - or none, which `guarantees()` reports. An unprivileged service user may not create
scopes, which is why the Pi uses cgroup delegation.

The backend checks at start-up that `bwrap` can really build its namespaces on this machine (a
kernel without unprivileged user namespaces, a systemd unit with `RestrictNamespaces=`, Ubuntu's
AppArmor user-namespace restriction): if not, it reports itself unavailable and the sandbox fails
closed - running unconfined is never an option. The argv is built by pure functions and
unit-tested on any OS; the real thing runs in CI on `ubuntu-24.04` and `ubuntu-24.04-arm`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nox.core.logging import get_logger
from nox.sandbox._cgroup import DelegatedCgroups
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)
from nox.sandbox.process import ProcessIo

log = get_logger(__name__)

__all__ = [
    "EGRESS_PORT",
    "Ceiling",
    "LinuxSandbox",
    "build_bwrap_argv",
    "bwrap_available",
    "forwarder_python",
]

_RO_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
#: Where the launcher and the proxy socket appear inside the sandbox (bwrap creates the folder).
_IN_SANDBOX_DIR = "/run/nox-sandbox"
_FORWARDER_IN = f"{_IN_SANDBOX_DIR}/egress_forwarder.py"
_SOCKET_IN = f"{_IN_SANDBOX_DIR}/egress.sock"
_FORWARDER_SRC = str(Path(__file__).with_name("_egress_forwarder.py"))
#: The proxy port on the sandbox's own loopback. Fixed: the namespace is new and empty, so
#: nothing else can hold it, and the environment can be built before the process starts.
EGRESS_PORT = 3128
_PROBE_ENV = {"PATH": "/usr/bin:/bin"}
_PROBE_TIMEOUT_S = 20.0
_UNAVAILABLE_HINT = (
    "unprivileged user namespaces are off, the service unit restricts namespaces or mounts, or "
    "Ubuntu's AppArmor user-namespace restriction applies - see docs/RASPBERRY_PI.md"
)


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def _under_system_dirs(path: str) -> bool:
    return any(path == d or path.startswith(d + "/") for d in _RO_SYSTEM)


def _is_within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def safe_python_binds(paths: list[str], *, private: list[str]) -> tuple[str, ...] | None:
    """The paths to bind for an interpreter outside `/usr`, or None when one is too wide.

    Each path must be a file or a folder of the interpreter itself; `/`, a home directory or
    anything that *contains* one of the `private` folders (the user's home, Nox's own folder with
    its secrets and database) would hand the sandbox far more than an interpreter, so the egress
    path is refused instead (fail closed).
    """
    out: list[str] = []
    for raw in paths:
        path = posixpath.normpath(raw)
        if path in ("/", "/home", "/root", "/var", "/opt", "/srv", "/tmp"):  # noqa: S108
            return None
        if posixpath.dirname(path) == "/home":  # /home/<user> itself
            return None
        if any(_is_within(folder, path) for folder in private if folder):
            return None
        if not _under_system_dirs(path) and path not in out:
            out.append(path)
    return tuple(out)


def _own_python_binds(real: str) -> tuple[str, ...] | None:
    """The minimum Nox's own interpreter needs inside the sandbox with `-I -S`: the executable,
    its standard library (with `lib-dynload`) and `libpython` when it is linked dynamically -
    never its prefix as a whole, which may be a home directory."""
    import sysconfig  # noqa: PLC0415 - only for this rare fallback

    from nox.paths import nox_home  # noqa: PLC0415

    paths = [real]
    for key in ("stdlib", "platstdlib"):
        folder = sysconfig.get_path(key)
        if folder and os.path.isdir(folder):
            paths.append(os.path.realpath(folder))
    libdir = sysconfig.get_config_var("LIBDIR") or ""
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        for name in {sysconfig.get_config_var("LDLIBRARY"), sysconfig.get_config_var("INSTSONAME")}:
            lib = os.path.join(libdir, str(name)) if name else ""
            if lib and os.path.isfile(lib):
                paths.append(lib)
                paths.append(os.path.realpath(lib))
    private = [os.path.expanduser("~"), str(nox_home())]
    return safe_python_binds(paths, private=private)


def forwarder_python() -> tuple[str, tuple[str, ...]] | None:
    """The interpreter that runs the egress launcher inside the sandbox, and what to bind for it.

    The system `python3` (always there on Raspberry Pi OS, Debian and Ubuntu; the installer makes
    sure) lives in the read-only `/usr` the sandbox already sees. Without one, Nox's own
    interpreter is used with only its executable, standard library and `libpython` bound
    read-only (`_own_python_binds`). `None` when neither works: declared egress then fails closed.
    """
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        if os.access(candidate, os.X_OK):
            return candidate, ()
    real = os.path.realpath(sys.executable) if sys.executable else ""
    if not real or not os.access(real, os.X_OK):
        return None
    if _under_system_dirs(real):
        return real, ()
    binds = _own_python_binds(real)
    if binds is None:
        log.warning("sandbox.forwarder_python_refused", python=real)
        return None
    return real, binds


def build_bwrap_argv(
    argv: list[str],
    *,
    work: str,
    egress_socket: str | None = None,
    python: str = "/usr/bin/python3",
    python_binds: tuple[str, ...] = (),
) -> list[str]:
    """The `bwrap` command line for one run. Pure, so it can be asserted in a unit test.

    `egress_socket` is the host path of the run's proxy socket when egress was declared: it is
    bound in and the target is wrapped by the forwarder (see the module docstring). Without it
    the process has no way out at all.

    The environment is deliberately *not* in it: a command line is world-readable
    (`/proc/<pid>/cmdline`, `ps`), and the child's environment carries its egress proxy's secret
    and possibly a server's settings. It is handed over as the process environment instead
    (`LinuxSandbox.environment`), which only the same user can read; bwrap passes it through.
    """
    cmd = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        # Always: a private network namespace with only its own loopback. Declared egress goes
        # through the proxy socket below, never through a network interface.
        "--unshare-net",
    ]
    for path in _RO_SYSTEM:
        # `--ro-bind-try` binds the path read-only when it exists and skips it otherwise, so the
        # argv is the same whatever the host looks like (and a missing /lib64 is not a hard error).
        cmd += ["--ro-bind-try", path, path]
    cmd += [
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",  # noqa: S108 - the sandbox's private /tmp, not the host's
        "--bind",
        work,
        work,
    ]
    if egress_socket is not None:
        for path in python_binds:
            cmd += ["--ro-bind", path, path]
        cmd += [
            "--ro-bind",
            _FORWARDER_SRC,
            _FORWARDER_IN,
            # Read-only is enough to connect(); the child cannot replace or remove it.
            "--ro-bind",
            egress_socket,
            _SOCKET_IN,
        ]
    cmd += ["--chdir", work, "--"]
    if egress_socket is not None:
        cmd += [python, "-I", "-S", _FORWARDER_IN, _SOCKET_IN, str(EGRESS_PORT), "--"]
    cmd += argv
    return cmd


def _probe(cmd: list[str], extra_env: Mapping[str, str] | None = None) -> str | None:
    """Run a start-up check; `None` when it worked, else why not (first line of stderr)."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv built in this module
            cmd,
            env={**_PROBE_ENV, **(extra_env or {})},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode == 0:
        return None
    text = done.stderr.decode("utf-8", errors="replace").strip()
    return text.splitlines()[0] if text else f"exit code {done.returncode}"


_PROBE_CLIENT = (
    "import socket\n"
    f"s = socket.create_connection(('127.0.0.1', {EGRESS_PORT}), 10)\n"
    "s.sendall(b'ping')\n"
    "print(s.recv(64).decode())\n"
)


def _egress_probe(python: str, binds: tuple[str, ...]) -> str | None:
    """Prove the egress path end to end on this machine: a child in the sandbox connects to its
    loopback port and must reach a Unix socket outside. `None` when it does, else why not."""
    if sys.platform == "win32":
        return "not on Linux"
    folder = tempfile.mkdtemp(prefix="nox-egress-probe-")
    path = os.path.join(folder, "egress.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        server.listen(1)
        server.settimeout(_PROBE_TIMEOUT_S)

        def answer() -> None:
            with contextlib.suppress(OSError):
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(_PROBE_TIMEOUT_S)
                    conn.sendall(b"pong:" + conn.recv(16))

        threading.Thread(target=answer, name="egress-probe", daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="nox-sbx-probe-") as work:
            cmd = build_bwrap_argv(
                [python, "-I", "-S", "-c", _PROBE_CLIENT],
                work=work,
                egress_socket=path,
                python=python,
                python_binds=binds,
            )
            try:
                done = subprocess.run(  # noqa: S603 - fixed argv built in this module
                    cmd,
                    env=dict(_PROBE_ENV),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=_PROBE_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return str(exc)
        if done.stdout.decode("utf-8", errors="replace").strip() == "pong:ping":
            return None
        text = done.stderr.decode("utf-8", errors="replace").strip()
        return text.splitlines()[-1] if text else f"exit code {done.returncode}"
    finally:
        server.close()
        shutil.rmtree(folder, ignore_errors=True)


#: Variables `systemd-run --user` needs to find the user manager; never passed into the sandbox.
_USER_BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _scope_prefix(limits: Limits, *, user: bool) -> list[str]:
    return [
        "systemd-run",
        *(["--user"] if user else []),
        "--scope",
        "--quiet",
        "--collect",
        "--no-ask-password",
        f"-pMemoryMax={limits.memory_bytes}",
        f"-pTasksMax={limits.max_processes}",
    ]


class Ceiling(StrEnum):
    """How the memory/process ceiling is enforced on this machine (first that works wins)."""

    #: The service's delegated cgroup subtree (`nox.service`, `Delegate=yes`) - the Pi.
    CGROUP = "cgroup"
    #: A transient scope of the user's own systemd manager - a desktop Linux session.
    USER_SCOPE = "user-scope"
    #: A transient system scope - only when the core runs as root.
    SCOPE = "scope"
    NONE = "none"


@dataclass(slots=True)
class _Launch:
    cmd: list[str]
    env: dict[str, str]
    work: str
    created: bool
    cgroup: Path | None

    def finish(self) -> None:
        """Kill what is left of the tree, drop its cgroup and its own work directory."""
        if self.cgroup is not None:
            DelegatedCgroups.remove(self.cgroup)
        if self.created:
            shutil.rmtree(self.work, ignore_errors=True)


class LinuxSandbox:
    """Backend for `nox.sandbox.Sandbox` on Linux and the Raspberry Pi."""

    #: The child has no network path to the host, so its proxy listens on a Unix socket.
    proxy_transport = "unix"

    def __init__(self, *, probe: bool = True) -> None:
        if not bwrap_available():
            raise SandboxUnavailable(
                "bwrap (bubblewrap) is not installed; the sandbox fails closed"
            )
        self.mode = SandboxMode.LINUX_BWRAP
        self._python = forwarder_python()
        self._cgroups: DelegatedCgroups | None = None
        self.ceiling = Ceiling.NONE
        if probe:
            with tempfile.TemporaryDirectory(prefix="nox-sbx-probe-") as work:
                why = _probe(build_bwrap_argv(["true"], work=work))
            if why is not None:
                raise SandboxUnavailable(
                    f"bwrap cannot build its sandbox here ({why}); {_UNAVAILABLE_HINT}"
                )
            if self._python is not None:
                egress_why = _egress_probe(*self._python)
                if egress_why is not None:
                    log.warning("sandbox.egress_unavailable", error=egress_why)
                    self._python = None
            self.ceiling = self._pick_ceiling()
        log.info(
            "sandbox.linux_ready",
            ceiling=self.ceiling.value,
            egress=self._python is not None,
        )

    def _pick_ceiling(self) -> Ceiling:
        """The first ceiling mechanism that really works here (each one is tried, not assumed)."""
        self._cgroups = DelegatedCgroups.setup()
        if self._cgroups is not None:
            try:
                DelegatedCgroups.remove(self._cgroups.create(Limits()))
                return Ceiling.CGROUP
            except OSError as exc:
                log.warning("sandbox.cgroup_unavailable", error=str(exc))
                self._cgroups = None
        if shutil.which("systemd-run") is None:
            return Ceiling.NONE
        if os.environ.get("XDG_RUNTIME_DIR") and (
            _probe([*_scope_prefix(Limits(), user=True), "true"], self._bus_env()) is None
        ):
            return Ceiling.USER_SCOPE
        if _probe([*_scope_prefix(Limits(), user=False), "true"]) is None:
            return Ceiling.SCOPE
        return Ceiling.NONE

    @staticmethod
    def _bus_env() -> dict[str, str]:
        return {k: os.environ[k] for k in _USER_BUS_ENV if os.environ.get(k)}

    def guarantees(self) -> Guarantees:
        notes: list[str] = []
        if self.ceiling is Ceiling.NONE:
            notes.append("no cgroup or systemd scope available: no memory/process ceiling")
        if self._python is None:
            notes.append("egress forwarder unavailable: declared egress is refused (fail closed)")
        return Guarantees(
            backend=self.mode.value,
            confines_writes=True,
            confines_reads=True,
            hides_secrets=True,
            confines_network=True,
            notes=tuple(notes),
        )

    def environment(
        self,
        work: str,
        proxy_port: int | None,
        extra: Mapping[str, str] | None = None,
        proxy_auth: str = "",
    ) -> dict[str, str]:
        """The child's whole environment. `proxy_port` is the port on the sandbox's own loopback
        (`EGRESS_PORT`), where the forwarder leads to the run's proxy."""
        # The caller's extra variables first, so the sandbox's own always win (reserved keys are
        # also refused up front by `checked_env`).
        env = dict(extra or {})
        env |= {"PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work}
        if proxy_port is not None:
            userinfo = f"{proxy_auth}@" if proxy_auth else ""
            url = f"http://{userinfo}127.0.0.1:{proxy_port}"
            for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
                env[key] = env[key.lower()] = url
        return env

    def command(
        self,
        argv: list[str],
        *,
        work: str,
        egress: EgressDecl,
        limits: Limits,
        proxy_socket: str | None = None,
        cgroup: Path | None = None,
    ) -> list[str]:
        """The full command line (the ceiling around `bwrap`) for one confined process.
        Never contains the environment - see `build_bwrap_argv`."""
        socket_path = self._egress_socket(egress, proxy_socket)
        python, binds = self._python or ("", ())
        inner = build_bwrap_argv(
            argv, work=work, egress_socket=socket_path, python=python, python_binds=binds
        )
        if self.ceiling is Ceiling.CGROUP and cgroup is not None:
            return DelegatedCgroups.wrap(cgroup, inner)
        if self.ceiling is Ceiling.USER_SCOPE:
            # systemd-run needs the bus variables; the confined process must not inherit them.
            unset = [arg for key in _USER_BUS_ENV for arg in ("--unsetenv", key)]
            inner = [inner[0], *unset, *inner[1:]]
            return [*_scope_prefix(limits, user=True), *inner]
        if self.ceiling is Ceiling.SCOPE:
            return [*_scope_prefix(limits, user=False), *inner]
        return inner

    def _egress_socket(self, egress: EgressDecl, proxy_socket: str | None) -> str | None:
        """The proxy socket to bind in, or `SandboxUnavailable` when egress cannot be honoured."""
        if not egress.wants_network:
            return None
        if not proxy_socket:
            raise SandboxUnavailable(
                "egress declared but no proxy socket is running; failing closed"
            )
        if self._python is None:
            raise SandboxUnavailable(
                "egress declared but the in-sandbox forwarder is unavailable here; failing closed"
            )
        return proxy_socket

    def _prepare(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        extra_env: Mapping[str, str] | None,
        proxy_auth: str,
        proxy_socket: str | None,
    ) -> _Launch:
        socket_path = self._egress_socket(egress, proxy_socket)  # refuse before creating anything
        cgroup: Path | None = None
        if self.ceiling is Ceiling.CGROUP and self._cgroups is not None:
            try:
                cgroup = self._cgroups.create(limits)
            except OSError as exc:
                # The ceiling was promised by guarantees(); without it, refuse (fail closed).
                raise SandboxUnavailable(f"could not create the run's cgroup: {exc}") from exc
        work = cwd or tempfile.mkdtemp(prefix="nox-sbx-")
        cmd = self.command(
            argv,
            work=work,
            egress=egress,
            limits=limits,
            proxy_socket=socket_path,
            cgroup=cgroup,
        )
        env = self.environment(
            work, EGRESS_PORT if socket_path is not None else None, extra_env, proxy_auth
        )
        if self.ceiling is Ceiling.USER_SCOPE:
            env |= self._bus_env()  # for systemd-run only; bwrap unsets them (see command)
        return _Launch(cmd, env, work, cwd is None, cgroup)

    async def _spawn(self, launch: _Launch, *, stdin: int) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *launch.cmd,
                env=launch.env,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            await asyncio.to_thread(launch.finish)
            raise SandboxUnavailable(f"could not start the sandboxed process: {exc}") from exc

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None = None,  # noqa: ARG002 - the proxy is a socket here, see Backend
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
    ) -> ProcessIo:
        """Start `argv` confined exactly like `run`, kept running with stdio pipes.

        `bwrap --die-with-parent` ties the tree to the core: if Nox dies, the kernel kills it.
        `--unshare-pid` makes the confined process PID 1 of its namespace, so killing `bwrap`
        ends every process it started (and with a cgroup ceiling, `cgroup.kill` does too).
        """
        launch = self._prepare(
            argv,
            cwd=cwd,
            egress=egress,
            limits=limits,
            extra_env=extra_env,
            proxy_auth=proxy_auth,
            proxy_socket=proxy_socket,
        )
        proc = await self._spawn(launch, stdin=asyncio.subprocess.PIPE)
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        if stdin is None or stdout is None or stderr is None:
            proc.kill()
            await asyncio.to_thread(launch.finish)
            raise SandboxUnavailable("the sandboxed process has no stdio pipes")

        async def write_in(data: bytes) -> None:
            stdin.write(data)
            await stdin.drain()

        def close_in() -> None:
            with contextlib.suppress(OSError, RuntimeError):
                stdin.close()

        async def wait() -> int | None:
            return await proc.wait()

        def kill() -> None:
            if launch.cgroup is not None:
                DelegatedCgroups.kill(launch.cgroup)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

        async def cleanup() -> None:
            close_in()
            await asyncio.to_thread(launch.finish)

        return ProcessIo(
            pid=proc.pid,
            read_out=stdout.read,
            read_err=stderr.read,
            write_in=write_in,
            close_in=close_in,
            wait=wait,
            kill=kill,
            cleanup=cleanup,
        )

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None = None,  # noqa: ARG002 - the proxy is a socket here, see Backend
        extra_env: Mapping[str, str] | None = None,
        proxy_auth: str = "",
        proxy_socket: str | None = None,
    ) -> Result:
        launch = self._prepare(
            argv,
            cwd=cwd,
            egress=egress,
            limits=limits,
            extra_env=extra_env,
            proxy_auth=proxy_auth,
            proxy_socket=proxy_socket,
        )
        proc = await self._spawn(launch, stdin=asyncio.subprocess.DEVNULL)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.timeout_s)
        except TimeoutError:
            timed_out = True
            if launch.cgroup is not None:
                DelegatedCgroups.kill(launch.cgroup)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out, err = b"", b""
        finally:
            await asyncio.to_thread(launch.finish)
        cap = limits.max_output_bytes
        return Result(
            exit_code=None if timed_out else proc.returncode,
            stdout=out[:cap].decode("utf-8", errors="replace"),
            stderr=err[:cap].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            truncated=len(out) > cap or len(err) > cap,
        )
