"""The Windows sandbox backend: a confined, hidden, resource-capped child process.

Two variants, strongest first:

* **AppContainer** (default when the profile can be created, which needs no administrator): the
  child runs as a container SID with no capabilities, so it cannot read the user's files, cannot
  reach the credential store, and has no network capability at all. The work directory is ACL'd to
  the container SID so that is the one place it can read and write.
* **Low-integrity token** (fallback): the child runs with a low-integrity copy of the core's token.
  Mandatory integrity control stops it writing anything labelled medium (every user file) and the
  low-IL socket restriction stops it opening the network. It can still *read* the user's files and
  the credential store, so this variant's `Guarantees` say so, and the dashboard shows the weaker
  state. It is used only when AppContainer is unavailable.

Network: a sandboxed child cannot open sockets. When a run declares egress, the proxy (started by
the caller) listens on loopback and the child's HTTP client is pointed at it through
`HTTPS_PROXY`/`HTTP_PROXY`; loopback is the one destination a low-IL/AppContainer socket may reach.
The firewall rule the installer adds is what stops a process from reaching anything *but* loopback;
without it we still pass the proxy address and rely on the socket restriction, and
`firewall_present()` lets the core warn.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

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

__all__ = ["WindowsSandbox", "appcontainer_available"]

_APP_CONTAINER_NAME = "Nox.Sandbox"
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


def appcontainer_available() -> bool:
    try:
        from nox.sandbox import _win32

        _win32.app_container_sid(_APP_CONTAINER_NAME)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "use the fallback"
        log.info("sandbox.appcontainer_unavailable", error=f"{type(exc).__name__}: {exc}")
        return False


class WindowsSandbox:
    """Backend for `nox.sandbox.Sandbox` on Windows. One instance per core."""

    def __init__(self, *, prefer_appcontainer: bool = True) -> None:
        from nox.sandbox import _win32

        self._w = _win32
        self._sid: str | None = None
        if prefer_appcontainer and appcontainer_available():
            self._sid = _win32.app_container_sid(_APP_CONTAINER_NAME)
        self.mode = SandboxMode.WINDOWS_APPCONTAINER if self._sid else SandboxMode.WINDOWS_LOW_IL

    def guarantees(self) -> Guarantees:
        if self._sid is not None:
            return Guarantees(
                backend=self.mode.value,
                confines_writes=True,
                confines_reads=True,
                hides_secrets=True,
                confines_network=True,
                notes=("AppContainer: no file, credential or network capability",),
            )
        return Guarantees(
            backend=self.mode.value,
            confines_writes=True,
            confines_reads=False,
            hides_secrets=False,
            confines_network=True,
            notes=(
                "low-integrity fallback: cannot write user files or open the network, but can "
                "still read user files and the credential store - install AppContainer support",
            ),
        )

    def _prepare_workdir(self, cwd: str | None) -> tuple[Path, bool]:
        if cwd is not None:
            return Path(cwd), False
        work = Path(tempfile.mkdtemp(prefix="nox-sbx-"))
        if self._sid is not None:
            # Grant the container SID full control of its one writable place.
            subprocess.run(  # noqa: S603 - fixed argv, our own temp path
                ["icacls", str(work), "/grant", f"*{self._sid}:(OI)(CI)F"],
                capture_output=True,
                check=False,
            )
        else:
            self._w.label_low(str(work))
        return work, True

    def _child_env(self, work: Path, proxy_port: int | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
        env["PATH"] = r"C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0"
        work_s = str(work)
        env["TEMP"] = env["TMP"] = work_s
        env["USERPROFILE"] = env["LOCALAPPDATA"] = env["APPDATA"] = work_s
        # PowerShell writes a module cache; keep it in the work dir, not the (unwritable) profile.
        env["PSModuleAnalysisCachePath"] = str(work / "psmac")
        if proxy_port is not None:
            url = f"http://127.0.0.1:{proxy_port}"
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
        proxy_port: int | None,
    ) -> Result:
        """Run `argv` confined; `proxy_port` is the loopback egress proxy when egress is wanted."""
        import asyncio

        if egress.wants_network and proxy_port is None:
            raise SandboxUnavailable(
                "egress declared but no proxy is running (firewall rule missing?); failing closed"
            )
        work, created = self._prepare_workdir(cwd)
        env = self._child_env(work, proxy_port if egress.wants_network else None)
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

        def _drain() -> tuple[int | None, bytes, bytes, bool]:
            cap = limits.max_output_bytes
            deadline_ms = int(limits.timeout_s * 1000)
            exit_code = spawned.wait(deadline_ms)
            timed_out = exit_code is None
            if timed_out:
                spawned.kill()
            out = os.read(spawned.stdout_fd, cap + 1) if spawned.stdout_fd else b""
            err = os.read(spawned.stderr_fd, cap + 1) if spawned.stderr_fd else b""
            return exit_code, out, err, timed_out

        try:
            exit_code, out, err, timed_out = await asyncio.to_thread(_drain)
        finally:
            spawned.close()
            if created:
                _best_effort_rmtree(work)
        return _result(exit_code, out, err, timed_out, limits.max_output_bytes)


def _result(exit_code: int | None, out: bytes, err: bytes, timed_out: bool, cap: int) -> Result:
    truncated = len(out) > cap or len(err) > cap
    return Result(
        exit_code=exit_code,
        stdout=out[:cap].decode("utf-8", errors="replace"),
        stderr=err[:cap].decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=truncated,
    )


def _best_effort_rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
