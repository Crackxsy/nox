"""Starting a registered program: the promises the starter has to keep.

This is the one place in Nox that runs something the user chose, so the tests are about the
guarantees rather than the happy path: a program that hangs is killed instead of hanging the
assistant with it, a program that is not there is reported by name, and a started program never
inherits Nox's own environment variables.

The test programs are this interpreter with a `-c` snippet, which is the only executable every
machine running the suite is guaranteed to have.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from nox.core.config.presets import PresetActionConfig
from nox.presets.actions import _child_environment, run_action


def action(script: str, **overrides: Any) -> PresetActionConfig:
    payload: dict[str, Any] = {
        "id": "probe",
        "name": "Probe",
        "command": [sys.executable, "-c", script],
        **overrides,
    }
    return PresetActionConfig.model_validate(payload)


@pytest.mark.asyncio
async def test_a_program_that_succeeds_reports_success() -> None:
    result = await run_action(action("pass"))

    assert result.ok
    assert result.exit_code == 0
    assert result.error == ""
    assert not result.timed_out


@pytest.mark.asyncio
async def test_a_non_zero_exit_is_a_failure_with_its_code() -> None:
    result = await run_action(action("import sys; sys.exit(3)"))

    assert not result.ok
    assert result.exit_code == 3
    assert "code 3" in result.error


@pytest.mark.asyncio
async def test_error_output_is_included_so_the_reason_is_visible() -> None:
    script = "import sys; sys.stderr.write('no such window'); sys.exit(1)"

    result = await run_action(action(script))

    assert not result.ok
    assert "no such window" in result.error


@pytest.mark.asyncio
async def test_a_hanging_program_is_stopped_at_its_deadline() -> None:
    """Without this, one bad entry would hold up every later step of the preset."""
    result = await run_action(action("import time; time.sleep(30)", timeout_s=0.5))

    assert not result.ok
    assert result.timed_out
    assert "0.5 s" in result.error
    assert result.duration_ms < 5_000


@pytest.mark.asyncio
async def test_a_missing_program_is_named_rather_than_crashing() -> None:
    missing = PresetActionConfig.model_validate(
        {"id": "ghost", "name": "Ghost", "command": ["C:/nowhere/at/all.exe"]}
    )

    result = await run_action(missing)

    assert not result.ok
    assert result.exit_code is None
    assert "C:/nowhere/at/all.exe" in result.error


@pytest.mark.asyncio
async def test_a_missing_working_directory_does_not_prevent_the_start() -> None:
    """A stale directory is a reason to start in the default one, not to refuse the action."""
    result = await run_action(action("pass", working_dir="C:/nowhere/at/all"))

    assert result.ok


def test_nox_own_variables_are_not_passed_to_the_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOX_INTERNAL_PROBE", "private")
    monkeypatch.setenv("PATH_PROBE", "ordinary")

    environment = _child_environment()

    assert "NOX_INTERNAL_PROBE" not in environment
    assert environment["PATH_PROBE"] == "ordinary"
    # The rest of the environment still reaches the program: a tool that needs PATH keeps working.
    assert len(environment) >= len(os.environ) - 1
