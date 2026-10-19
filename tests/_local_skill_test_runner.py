"""Test-only: the skill test runner in a plain subprocess, for the repository's own fixtures.

Production never runs a skill's tests outside the sandbox (`nox.skills.testing` has no such
runner). The unit tests still want to exercise the runner script and the service around it on
machines without a sandbox, with the fixture skills this repository writes itself
(`tests._fake_skill_builder`) - never with model-written code.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from nox.plugins.manager import worker_environment
from nox.skills.testing import RUNNER_FILE, TestOutcome, _prepare, count_tests, parse_outcome


class LocalTestRunner:
    sandboxed = False

    def unavailable(self) -> str | None:
        return None

    async def run(self, plugin_dir: Path, *, timeout_s: float) -> TestOutcome:
        run_dir = plugin_dir.parent / f"{plugin_dir.name}.testrun"
        await asyncio.to_thread(_prepare, plugin_dir, run_dir)
        env = {k: v for k, v in worker_environment().items() if k != "PYTHONPATH"}
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            RUNNER_FILE,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(run_dir),
            env=env,
            **kwargs,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout_s)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            out, err = await process.communicate()
        finally:
            await asyncio.to_thread(shutil.rmtree, run_dir, True)
        return parse_outcome(
            (out or b"").decode("utf-8", errors="replace"),
            (err or b"").decode("utf-8", errors="replace"),
            exit_code=process.returncode,
            timed_out=timed_out,
            expected=count_tests(plugin_dir),
        )
