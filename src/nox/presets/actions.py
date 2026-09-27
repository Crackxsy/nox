"""Running one registered action - the only place in Nox that starts a program the user chose.

Three properties make this safe enough to exist at all, and all three are enforced here rather
than asked of the caller:

* No shell. The command is a list and reaches the operating system as a list, so there is no
  quoting to get wrong and no separator that could append a second command.
* No caller-supplied command. `run_action` takes a `PresetActionConfig` that came from the
  configuration file; nothing in the signature accepts a path or an argument from a model, from
  IPC, or from a spoken sentence.
* A deadline. A program that hangs is killed, and the caller is told that it was killed rather
  than being left waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from nox.core.config.presets import PresetActionConfig
from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["ActionResult", "run_action"]

#: Keep the program's console window from flashing over whatever the user is doing. Only exists
#: on Windows; elsewhere (the test suite on a Linux runner) there is no flag to pass.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

#: How much of the program's error output is worth showing. Enough to recognise the problem,
#: short enough to say out loud.
_ERROR_EXCERPT_CHARS = 400

#: Variables that belong to Nox itself are not passed on. Nothing secret is kept in the
#: environment - secrets live in the Windows Credential Manager - but an action is a foreign
#: program, and it has no business reading Nox's own settings.
_PRIVATE_PREFIX = "NOX_"


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What happened when the program ran."""

    ok: bool
    exit_code: int | None
    #: One sentence for the user when something went wrong, empty when it did not.
    error: str
    duration_ms: float
    timed_out: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
            "timed_out": self.timed_out,
        }


def _child_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith(_PRIVATE_PREFIX)}


def _working_directory(action: PresetActionConfig) -> str | None:
    if not action.working_dir:
        return None
    directory = Path(action.working_dir)
    return str(directory) if directory.is_dir() else None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """End a process that outstayed its deadline, without waiting on it forever in turn."""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2.0)


async def run_action(action: PresetActionConfig) -> ActionResult:
    """Start one registered action and wait for it, up to its own timeout."""
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    try:
        process = await asyncio.create_subprocess_exec(
            *action.command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=_working_directory(action),
            env=_child_environment(),
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        log.warning("presets.action_missing", action=action.id, program=action.command[0])
        return ActionResult(
            ok=False,
            exit_code=None,
            error=f"the program for {action.name!r} was not found: {action.command[0]}",
            duration_ms=elapsed_ms(),
        )
    except OSError as exc:
        log.warning("presets.action_start_failed", action=action.id, error=str(exc))
        return ActionResult(
            ok=False,
            exit_code=None,
            error=f"{action.name!r} could not be started: {exc.strerror or exc}",
            duration_ms=elapsed_ms(),
        )

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=action.timeout_s)
    except TimeoutError:
        await _terminate(process)
        log.warning("presets.action_timeout", action=action.id, timeout_s=action.timeout_s)
        return ActionResult(
            ok=False,
            exit_code=None,
            error=f"{action.name!r} did not finish within {action.timeout_s:g} s and was stopped",
            duration_ms=elapsed_ms(),
            timed_out=True,
        )

    if process.returncode == 0:
        log.info("presets.action_ran", action=action.id, duration_ms=round(elapsed_ms(), 1))
        return ActionResult(ok=True, exit_code=0, error="", duration_ms=elapsed_ms())

    detail = stderr.decode("utf-8", errors="replace").strip()[:_ERROR_EXCERPT_CHARS]
    log.warning("presets.action_failed", action=action.id, exit_code=process.returncode)
    return ActionResult(
        ok=False,
        exit_code=process.returncode,
        error=(
            f"{action.name!r} ended with code {process.returncode}"
            + (f": {detail}" if detail else "")
        ),
        duration_ms=elapsed_ms(),
    )
