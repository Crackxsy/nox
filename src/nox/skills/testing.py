"""Running a self-built skill's tests: inside the sandbox, or not at all.

The tests are code a model wrote a minute ago, so they run exactly where the skill itself will run:
in `nox.sandbox` (AppContainer on Windows, `bwrap` on Linux), with no egress declared, a time limit,
a fixed argv, nothing inherited, and the socket layer replaced by one that refuses every connection
before the first test is imported. The static checks (`nox.skills.checks`) have already refused
anything that could undo that from inside.

**There is no fallback.** If no sandbox that hides the user's files and the credential store can be
built here, `default_test_runner()` returns `UnavailableTestRunner`: learning is refused up front
with an honest reason, and every run is a failure. If the sandboxed interpreter does not get as far
as writing its verdict - it could not start, it was killed, the test code ended the process - the
outcome is a failure too. Nothing ever re-runs model-written code outside the sandbox.

The runner is a small, dependency-free one written into the run folder: every `tests/test_*.py`
module, every `test_*` function, `async def` ones run with `asyncio.run`. No pytest - a production
install of Nox does not carry it, and a skill should not need it.

**The test verdict is advisory, not a security control.** The same model writes the skill and its
tests, so a hostile skill can always ship tests that pass trivially; no construction around the
verdict changes that. What protects the user is that the skill runs only in the sandbox (its tests
and its worker alike), the static checks, and the user's activation with an honest summary that
says the tests were written by the skill itself. The runner therefore keeps only the simple, robust
rules: it writes its `RESULT` trailer after a normal return from the last test, so a premature exit
(`SystemExit`, `os._exit`, a kill) is a failure; exactly one trailer and exit code 0 are required.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nox.core.logging import get_logger
from nox.skills.bundle import BOOTSTRAP, BundleError, bundle_files, encode_bundle

log = get_logger(__name__)

__all__ = [
    "NO_SANDBOX",
    "SandboxTestRunner",
    "TestOutcome",
    "TestRunner",
    "UnavailableTestRunner",
    "count_tests",
    "default_test_runner",
    "parse_outcome",
    "strong_sandbox",
]

OUTPUT_CHARS = 4000
RUNNER_FILE = "_nox_skill_tests.py"

#: Written into the run folder and started as `python -I _nox_skill_tests.py` inside the sandbox.
RUNNER_SOURCE = r"""
import asyncio, importlib.util, inspect, os, socket, sys, traceback
from pathlib import Path


def main():
    def refuse(*args, **kwargs):
        raise OSError("network access is not available while a skill is tested")

    for name in ("connect", "connect_ex", "sendto", "bind"):
        setattr(socket.socket, name, refuse)
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    socket.socketpair = refuse

    root = Path.cwd()
    sys.path.insert(0, str(root / "src"))
    sys.path.insert(0, str(root))
    passed = failed = 0
    try:
        for path in sorted((root / "tests").glob("test_*.py")):
            spec = importlib.util.spec_from_file_location("skilltest_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except BaseException:
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
                except BaseException:
                    failed += 1
                    print("FAIL", path.name, name)
                    traceback.print_exc(limit=3)
    finally:
        sys.stdout.flush()
        print("RESULT passed=%d failed=%d" % (passed, failed), flush=True)
    return 1 if failed or not passed else 0


sys.exit(main())
"""

NO_SANDBOX = (
    "I cannot learn new skills on this computer: their code may only run in a sandbox that hides "
    "your files and passwords, and none is available here"
)


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

    def unavailable(self) -> str | None:
        """Why no test can run here (learning is then refused up front), or None."""

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome: ...


def parse_outcome(
    stdout: str,
    stderr: str,
    *,
    exit_code: int | None,
    timed_out: bool,
    expected: int | None = None,
) -> TestOutcome:
    """The advisory verdict from the runner's trailer (fail closed on anything unusual).

    Exactly one `RESULT` line and exit code 0 are required. No line - the interpreter did not
    start, the process was killed, a test ended it with `os._exit` - or a second one (a test
    printed its own) is a failed run, and so is a non-zero exit code whatever the line says.
    `expected` is the number of `test_*` functions the core found in the files; a trailer that
    accounts for a different number is a failure too.
    """
    lines = [line for line in stdout.splitlines() if line.startswith("RESULT ")]
    output = f"{stdout}\n{stderr}".strip()
    if len(lines) != 1:
        output = f"{output}\n(the tests did not report exactly one result inside the sandbox)"
        return TestOutcome(0, 1, output[-OUTPUT_CHARS:], timed_out)
    fields = dict(part.split("=", 1) for part in lines[0][len("RESULT ") :].split() if "=" in part)
    try:
        passed, failed = int(fields.get("passed", 0)), int(fields.get("failed", 0))
    except ValueError:
        passed, failed = 0, 1
    if expected is not None and passed + failed != expected:
        output = f"{output}\n(the result counts {passed + failed} tests; the files hold {expected})"
        failed = max(failed, 1)
    if exit_code != 0 and failed == 0:
        failed = 1
    return TestOutcome(passed, failed, output[-OUTPUT_CHARS:], timed_out)


def count_tests(plugin_dir: Path) -> int:
    """`test_*` functions defined at the top level of `tests/test_*.py`, found statically."""
    total = 0
    for path in sorted((plugin_dir / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        total += sum(
            1
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    return total


def _prepare(plugin_dir: Path, run_dir: Path) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    shutil.copytree(plugin_dir, run_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (run_dir / RUNNER_FILE).write_text(RUNNER_SOURCE, encoding="utf-8")


class SandboxTestRunner:
    """The runner inside `nox.sandbox`, through `Sandbox.start(argv, egress=NONE, limits, ...)`.

    The sandbox creates the process's private work folder; the skill's files and the runner reach
    it as a bundle on stdin (`nox.skills.bundle`), so no folder under `<nox home>` is ever shared.
    Never falls back to anything.
    """

    sandboxed = True

    def __init__(self, sandbox: Any, module: Any, python: str | None = None) -> None:
        self._sandbox = sandbox
        self._module = module
        self._python = python or sys.executable

    def unavailable(self) -> str | None:
        return None

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome:
        try:
            files = await asyncio.to_thread(bundle_files, plugin_dir, {RUNNER_FILE: RUNNER_SOURCE})
            data = encode_bundle(files)
        except BundleError as exc:
            return TestOutcome(0, 1, f"the skill cannot be handed to the sandbox: {exc}")
        expected = await asyncio.to_thread(count_tests, plugin_dir)
        try:
            process = await self._sandbox.start(
                [self._python, "-I", "-c", BOOTSTRAP, RUNNER_FILE],
                egress=self._module.EgressDecl(),
                limits=self._module.Limits(timeout_s=timeout_s),
                # The skill's own sandbox container: no other workload can see its work folder.
                workload=f"skill:{plugin_dir.name}",
                lifetime_s=timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - the sandbox refused: that is a failed run
            log.warning("skills.sandbox_refused_tests", error=f"{type(exc).__name__}: {exc}")
            return TestOutcome(0, 1, f"the sandbox could not run the tests: {type(exc).__name__}")
        stdout, code, timed_out = await _collect(process, data, timeout_s)
        outcome = parse_outcome(
            stdout,
            str(process.stderr_tail()),
            exit_code=code,
            timed_out=timed_out,
            expected=expected,
        )
        log.info("skills.tests_ran", passed=outcome.passed, failed=outcome.failed, sandbox=True)
        return outcome


async def _collect(process: Any, data: bytes, timeout_s: float) -> tuple[str, int | None, bool]:
    """Send the bundle, read stdout to its end, wait for the exit; kill on the time limit."""
    chunks: list[bytes] = []
    size = 0

    async def read_all() -> None:
        nonlocal size
        await process.send(data)
        while line := await process.readline():
            size += len(line)
            if size <= OUTPUT_CHARS * 4:
                chunks.append(line)

    timed_out = False
    try:
        await asyncio.wait_for(read_all(), timeout_s)
        code = await process.wait(timeout=10.0)
    except (TimeoutError, OSError, ValueError) as exc:
        timed_out = isinstance(exc, TimeoutError)
        code = None
    finally:
        await process.stop(grace_s=1.0)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text, code, timed_out


class UnavailableTestRunner:
    """No sandbox here: nothing runs, every outcome is a failure, learning is refused."""

    sandboxed = False

    def __init__(self, reason: str = NO_SANDBOX) -> None:
        self.reason = reason

    def unavailable(self) -> str | None:
        return self.reason

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome:
        del plugin_dir, timeout_s
        return TestOutcome(0, 1, self.reason)


def strong_sandbox(sandbox: Any) -> bool:
    """Whether `sandbox` confines writes, reads, the credential store and the network.

    The Windows low-integrity fallback can still read the user's files and secrets, so it does not
    qualify for code a model wrote.
    """
    try:
        return bool(sandbox.guarantees().strong)
    except Exception:  # noqa: BLE001 - one that cannot say what it guarantees guarantees nothing
        return False


def default_test_runner(sandbox_factory: Callable[[], Any] | None = None) -> TestRunner:
    """A strong sandbox when one can be built here, else `UnavailableTestRunner` (fail closed)."""
    try:
        module = importlib.import_module("nox.sandbox")
        sandbox = (sandbox_factory or module.make_sandbox)()
    except Exception as exc:  # noqa: BLE001 - no backend here: nothing may run
        log.warning("skills.sandbox_unavailable", error=f"{type(exc).__name__}: {exc}")
        return UnavailableTestRunner()
    if sandbox is None or not strong_sandbox(sandbox):
        log.warning("skills.sandbox_too_weak")
        return UnavailableTestRunner()
    return SandboxTestRunner(sandbox, module)
