"""The Linux / Raspberry Pi sandbox backend: `bwrap` for isolation, `systemd-run` for limits.

`bubblewrap` (`bwrap`) builds the view the process gets: the system mounted read-only, the work
directory the only writable place, a private `/tmp`, no access to the user's home. Network:

* no declared egress ⇒ `--unshare-net`, so the process has no network namespace at all;
* declared egress ⇒ a network namespace that can reach only the loopback proxy. In Wave 1 that is
  done by keeping loopback (`--share-net` is too wide) - the proxy listens on `127.0.0.1` and the
  process reaches it there, while the firewall/nftables rule the installer adds blocks every other
  outbound packet from the sandbox's cgroup. Without that rule we fail closed when egress is asked
  for, exactly like Windows without its firewall rule.

`systemd-run --scope -p MemoryMax= -p TasksMax=` wraps the whole thing so the Pi's systemd kills it
on the memory or process ceiling, and the scope dies with the core. If `bwrap` is not installed the
backend reports itself unavailable and the sandbox fails closed - running unconfined is never an
option. This module builds the argv and is unit-tested on that; it is exercised for real only on
Linux (CI's `ubuntu-24.04` and `ubuntu-24.04-arm`), never on the Windows dev box.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Mapping

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

log = get_logger(__name__)

__all__ = ["LinuxSandbox", "bwrap_available", "build_bwrap_argv"]

_RO_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def build_bwrap_argv(
    argv: list[str],
    *,
    work: str,
    env: dict[str, str],
    share_loopback: bool,
) -> list[str]:
    """The `bwrap` command line for one run. Pure, so it can be asserted in a unit test."""
    cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc"]
    if not share_loopback:
        cmd += ["--unshare-net"]
    else:
        # A private net namespace with only loopback up: the proxy is on 127.0.0.1, and nothing
        # else is reachable. The installer's nftables rule blocks the rest at the host edge.
        cmd += ["--unshare-net", "--dev", "/dev"]
    for path in _RO_SYSTEM:
        # `--ro-bind-try` binds the path read-only when it exists and skips it otherwise, so the
        # argv is the same whatever the host looks like (and a missing /lib64 is not a hard error).
        cmd += ["--ro-bind-try", path, path]
    cmd += [
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",  # noqa: S108 - the sandbox's private /tmp, not the host's
        "--bind",
        work,
        work,
        "--chdir",
        work,
        "--clearenv",
    ]
    for key, value in sorted(env.items()):
        cmd += ["--setenv", key, value]
    cmd += ["--"]
    cmd += argv
    return cmd


class LinuxSandbox:
    """Backend for `nox.sandbox.Sandbox` on Linux and the Raspberry Pi."""

    def __init__(self) -> None:
        if not bwrap_available():
            raise SandboxUnavailable("bwrap is not installed; the sandbox fails closed")
        self._has_systemd_run = shutil.which("systemd-run") is not None
        self.mode = SandboxMode.LINUX_BWRAP

    def guarantees(self) -> Guarantees:
        notes = () if self._has_systemd_run else ("systemd-run missing: no memory/process ceiling",)
        return Guarantees(
            backend=self.mode.value,
            confines_writes=True,
            confines_reads=True,
            hides_secrets=True,
            confines_network=True,
            notes=notes,
        )

    def _env(
        self, work: str, proxy_port: int | None, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        # The caller's extra variables first, so the sandbox's own always win (reserved keys are
        # also refused up front by `checked_env`).
        env = dict(extra or {})
        env |= {"PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work}
        if proxy_port is not None:
            url = f"http://127.0.0.1:{proxy_port}"
            env |= {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url}
        return env

    def command(
        self,
        argv: list[str],
        *,
        work: str,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,
        extra_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The full command line (`systemd-run` scope around `bwrap`) for one confined process."""
        inner = build_bwrap_argv(
            argv,
            work=work,
            env=self._env(work, proxy_port if egress.wants_network else None, extra_env),
            share_loopback=egress.wants_network,
        )
        if not self._has_systemd_run:
            return inner
        return [
            "systemd-run",
            "--scope",
            "--quiet",
            "--collect",
            f"-pMemoryMax={limits.memory_bytes}",
            f"-pTasksMax={limits.max_processes}",
            *inner,
        ]

    async def start(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,
        extra_env: Mapping[str, str] | None = None,
        proxy_socket: str | None = None,  # noqa: ARG002 - loopback TCP here, see Backend
    ) -> ProcessIo:
        """Start `argv` confined exactly like `run`, kept running with stdio pipes.

        `bwrap --die-with-parent` ties the tree to the core: if Nox dies, the kernel kills it.
        `--unshare-pid` makes the confined process PID 1 of its namespace, so killing `bwrap`
        ends every process it started.
        """
        if egress.wants_network and proxy_port is None:
            raise SandboxUnavailable("egress declared but no proxy is running; failing closed")
        work = cwd or tempfile.mkdtemp(prefix="nox-sbx-")
        created = cwd is None
        cmd = self.command(
            argv,
            work=work,
            egress=egress,
            limits=limits,
            proxy_port=proxy_port,
            extra_env=extra_env,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            if created:
                shutil.rmtree(work, ignore_errors=True)
            raise SandboxUnavailable(f"could not start the sandboxed process: {exc}") from exc
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        if stdin is None or stdout is None or stderr is None:
            proc.kill()
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
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

        async def cleanup() -> None:
            close_in()
            if created:
                await asyncio.to_thread(shutil.rmtree, work, True)

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
        proxy_port: int | None,
        extra_env: Mapping[str, str] | None = None,
        proxy_socket: str | None = None,  # noqa: ARG002 - loopback TCP here, see Backend
    ) -> Result:
        if egress.wants_network and proxy_port is None:
            raise SandboxUnavailable("egress declared but no proxy is running; failing closed")
        work = cwd or tempfile.mkdtemp(prefix="nox-sbx-")
        created = cwd is None
        cmd = self.command(
            argv,
            work=work,
            egress=egress,
            limits=limits,
            proxy_port=proxy_port,
            extra_env=extra_env,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limits.timeout_s)
        except TimeoutError:
            timed_out = True
            proc.kill()
            out, err = b"", b""
        finally:
            if created:
                shutil.rmtree(work, ignore_errors=True)
        cap = limits.max_output_bytes
        return Result(
            exit_code=None if timed_out else proc.returncode,
            stdout=out[:cap].decode("utf-8", errors="replace"),
            stderr=err[:cap].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            truncated=len(out) > cap or len(err) > cap,
        )
