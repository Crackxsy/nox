"""Running a self-built skill's tests: a fixed argv, no network, nothing inherited.

The tests are code the model wrote, so they are run like the skill itself would be: in a separate
process with the plugin-worker environment allow-list (minus `PYTHONPATH`), Python's isolated
mode, a time limit, no console window, and the socket layer replaced by one that refuses every
connection before the first test is imported. The static checks (`nox.skills.checks`) have
already refused anything that could undo that from inside.

The runner is a small, dependency-free one written into the run folder: every `tests/test_*.py`
module, every `test_*` function, `async def` ones run with `asyncio.run`. No pytest - a production
install of Nox does not carry it, and a skill should not need it.

When the sandbox (`nox.sandbox`, `make_sandbox()`) can be built, `SandboxTestRunner` runs the same
argv inside it - work folder confined, no egress declared - and `default_test_runner()` prefers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.plugins.manager import worker_environment

log = get_logger(__name__)

__all__ = [
    "ProcessTestRunner",
    "SandboxTestRunner",
    "TestOutcome",
    "TestRunner",
    "default_test_runner",
]

OUTPUT_CHARS = 4000
RUNNER_FILE = "_nox_skill_tests.py"

#: Written into the run folder and started as `python -I _nox_skill_tests.py <src>`.
RUNNER_SOURCE = """\
import asyncio, importlib.util, inspect, socket, sys, traceback
from pathlib import Path


def _refuse(*_a, **_k):
    raise OSError("network access is not available while a skill is tested")


for _name in ("connect", "connect_ex", "sendto", "bind"):
    setattr(socket.socket, _name, _refuse)
socket.create_connection = _refuse
socket.getaddrinfo = _refuse
socket.socketpair = _refuse

root = Path.cwd()
sys.path.insert(0, str(root / "src"))
sys.path.insert(0, str(root))
passed = failed = 0
for path in sorted((root / "tests").glob("test_*.py")):
    spec = importlib.util.spec_from_file_location("skilltest_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        failed += 1
        print("ERROR importing", path.name)
        traceback.print_exc(limit=3)
        continue
    for name, func in sorted(vars(module).items()):
        if not (name.startswith("test_") and callable(func)):
            continue
        try:
            result = func()
            if inspect.iscoroutine(result):
                asyncio.run(result)
            passed += 1
            print("PASS", path.name, name)
        except Exception:
            failed += 1
            print("FAIL", path.name, name)
            traceback.print_exc(limit=3)
print(f"RESULT passed={passed} failed={failed}")
sys.exit(1 if failed or not passed else 0)
"""


@dataclass(frozen=True, slots=True)
class TestOutcome:
    passed: int
    failed: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.passed > 0 and not self.timed_out


class TestRunner(Protocol):
    @property
    def sandboxed(self) -> bool: ...

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome: ...


def _parse(output: str, timed_out: bool) -> TestOutcome:
    passed = failed = 0
    for line in output.splitlines():
        if line.startswith("RESULT "):
            fields = dict(part.split("=", 1) for part in line[7:].split() if "=" in part)
            passed, failed = int(fields.get("passed", 0)), int(fields.get("failed", 0))
    if not timed_out and passed == 0 and failed == 0:
        failed = 1  # the runner never got to its summary line
    return TestOutcome(passed, failed, output[-OUTPUT_CHARS:], timed_out)


class ProcessTestRunner:
    """A separate isolated Python process; see the module docstring for what it is not."""

    sandboxed = False

    def __init__(self, python: str | None = None) -> None:
        self._python = python or sys.executable

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome:
        run_dir = plugin_dir.parent / f"{plugin_dir.name}.testrun"
        await asyncio.to_thread(_prepare, plugin_dir, run_dir)
        env = {k: v for k, v in worker_environment().items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            self._python,
            "-I",
            RUNNER_FILE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(run_dir),
            env=env,
            **kwargs,
        )
        timed_out = False
        try:
            out, _ = await asyncio.wait_for(process.communicate(), timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            out, _ = await process.communicate()
        finally:
            await asyncio.to_thread(shutil.rmtree, run_dir, True)
        text = (out or b"").decode("utf-8", errors="replace")
        outcome = _parse(text, timed_out)
        log.info(
            "skills.tests_ran", passed=outcome.passed, failed=outcome.failed, timeout=timed_out
        )
        return outcome


def _prepare(plugin_dir: Path, run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    shutil.copytree(plugin_dir, run_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (run_dir / RUNNER_FILE).write_text(RUNNER_SOURCE, encoding="utf-8")


class SandboxTestRunner:
    """The same runner, inside `nox.sandbox`: `Sandbox.run(argv, cwd=..., egress=NONE, limits)`.

    An adapter over the sandbox's public interface (`run(argv, *, cwd, egress, limits) -> Result`
    with `exit_code`, `stdout`, `stderr`, `timed_out`), so this module never imports it on a
    checkout without it.
    """

    sandboxed = True

    def __init__(self, sandbox: Any, module: Any, python: str | None = None) -> None:
        self._sandbox = sandbox
        self._module = module
        self._python = python or sys.executable

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome:
        run_dir = plugin_dir.parent / f"{plugin_dir.name}.testrun"
        await asyncio.to_thread(_prepare, plugin_dir, run_dir)
        try:
            result = await self._sandbox.run(
                [self._python, "-I", RUNNER_FILE],
                cwd=str(run_dir),
                egress=self._module.EgressDecl(),
                limits=self._module.Limits(timeout_s=timeout_s),
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, run_dir, True)
        text = f"{result.stdout}\n{result.stderr}"
        outcome = _parse(text, bool(result.timed_out))
        log.info("skills.tests_ran", passed=outcome.passed, failed=outcome.failed, sandbox=True)
        return outcome


def default_test_runner() -> TestRunner:
    """The sandbox when one can be built here, else the isolated-process runner."""
    try:
        module = importlib.import_module("nox.sandbox")
        sandbox = module.make_sandbox()
    except ImportError:
        return ProcessTestRunner()
    except Exception as exc:  # noqa: BLE001 - no backend here; the process runner remains
        log.warning("skills.sandbox_unavailable", error=f"{type(exc).__name__}: {exc}")
        return ProcessTestRunner()
    return SandboxTestRunner(sandbox, module)
