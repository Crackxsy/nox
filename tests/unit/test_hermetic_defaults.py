"""A bare `pytest` is hermetic: it never reaches real services, devices or toolchains.

Tests marked `e2e` (builds the UIs with npm, drives a browser), `network` (real OBS, Home
Assistant, Ollama, the Claude CLI, the OS keyring), `hardware` (microphone, speaker, GPU) and
`spike` (measurements) only run when asked for with `-m` - CI does so in the jobs that need them.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _default_mark_expression() -> str:
    options = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    args = shlex.split(options["addopts"])
    assert "-m" in args, "pytest addopts must deselect the non-hermetic markers by default"
    return args[args.index("-m") + 1]


@pytest.mark.parametrize("marker", ["e2e", "network", "hardware", "spike"])
def test_a_bare_run_deselects_tests_that_leave_the_machine(marker: str) -> None:
    expression = Expression.compile(_default_mark_expression())
    assert not expression.evaluate(lambda name, **_kw: name == marker)


def test_a_bare_run_still_selects_ordinary_tests() -> None:
    expression = Expression.compile(_default_mark_expression())
    assert expression.evaluate(lambda _name, **_kw: False)
    assert expression.evaluate(lambda name, **_kw: name in {"asyncio", "timeout"})
