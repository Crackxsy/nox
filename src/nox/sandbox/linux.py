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
import shutil
import tempfile

from nox.core.logging import get_logger
from nox.sandbox.model import (
    EgressDecl,
    Guarantees,
    Limits,
    Result,
    SandboxMode,
    SandboxUnavailable,
)

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

    def _env(self, work: str, proxy_port: int | None) -> dict[str, str]:
        env = {"PATH": "/usr/bin:/bin", "HOME": work, "TMPDIR": work}
        if proxy_port is not None:
            url = f"http://127.0.0.1:{proxy_port}"
            env |= {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url}
        return env

    async def run(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        egress: EgressDecl,
        limits: Limits,
        proxy_port: int | None,
        proxy_socket: str | None = None,
    ) -> Result:
        if egress.wants_network and proxy_port is None:
            raise SandboxUnavailable("egress declared but no proxy is running; failing closed")
        work = cwd or tempfile.mkdtemp(prefix="nox-sbx-")
        created = cwd is None
        inner = build_bwrap_argv(
            argv,
            work=work,
            env=self._env(work, proxy_port if egress.wants_network else None),
            share_loopback=egress.wants_network,
        )
        cmd = inner
        if self._has_systemd_run:
            cmd = [
                "systemd-run",
                "--scope",
                "--quiet",
                "--collect",
                f"-pMemoryMax={limits.memory_bytes}",
                f"-pTasksMax={limits.max_processes}",
                *inner,
            ]
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
